#!/usr/bin/env bash
# Provision a Hetzner Cloud server for a scaffolded tenant.
#
# Per SPEC-017 (Step 7b): closes the gap between new-tenant.sh (scaffolding)
# and pyinfra deploy (configuration). Creates the Hetzner box, attaches the
# right SSH key + firewall, waits for SSH, bootstraps the non-root claude
# user, and updates the tenant's tenant.yaml with real IP/hostname/server-id.
#
# Usage:
#   ./scripts/provision-tenant.sh <tenant-name> [--type=cx33] [--region=fsn1] \
#       [--image=ubuntu-24.04] [--ssh-key=<name-or-id>] [--dry-run]
#
# Examples:
#   ./scripts/provision-tenant.sh acme-corp
#   ./scripts/provision-tenant.sh acme-corp --type=cx33 --region=fsn1 --dry-run
#
# Requirements:
#   - hcloud CLI (brew install hcloud)
#   - jq (brew install jq)
#   - HCLOUD_TOKEN in macOS Keychain: service="hetzner-cloud", account="api_token"
#   - Existing Hetzner SSH key (default: first key matching $BUBBLE_SSH_KEY_FILTER,
#     or "operator" if unset; case-insensitive). Override with --ssh-key=NAME.
#   - Existing Hetzner Firewall named "bubble-default"
#   - Tenant scaffolding from new-tenant.sh already in $BUBBLE_DATA_REPO/tenants/<name>/
#
# Boilerplate part of bubble-vps-platform — works for any new tenant.

set -euo pipefail

# ─── Colors ─────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'
    RED=$'\033[0;31m'
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    NC=$'\033[0m'
else
    GREEN="" YELLOW="" RED="" BOLD="" DIM="" NC=""
fi
info()    { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn()    { printf "${YELLOW}!${NC} %s\n" "$*"; }
error()   { printf "${RED}✗${NC} %s\n" "$*" >&2; }
heading() { printf "\n${BOLD}%s${NC}\n" "$*"; }

# ─── Argument parsing ──────────────────────────────────────────────────────
TENANT_NAME=""
SERVER_TYPE="cx33"
REGION="fsn1"
IMAGE="ubuntu-24.04"
SSH_KEY_FILTER="${BUBBLE_SSH_KEY_FILTER:-operator}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --type=*)     SERVER_TYPE="${1#--type=}"; shift ;;
        --region=*)   REGION="${1#--region=}"; shift ;;
        --image=*)    IMAGE="${1#--image=}"; shift ;;
        --ssh-key=*)  SSH_KEY_FILTER="${1#--ssh-key=}"; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --help|-h)
            grep '^#' "$0" | head -30 | sed 's/^# //;s/^#//'
            exit 0
            ;;
        --*)
            error "unknown flag: $1"
            exit 2
            ;;
        *)
            if [[ -z "$TENANT_NAME" ]]; then
                TENANT_NAME="$1"
                shift
            else
                error "unexpected positional arg: $1 (tenant-name already set to '$TENANT_NAME')"
                exit 2
            fi
            ;;
    esac
done

# ─── Validate args ─────────────────────────────────────────────────────────
if [[ -z "$TENANT_NAME" ]]; then
    error "missing <tenant-name> positional argument"
    error "usage: $0 <tenant-name> [--type=cx33] [--region=fsn1] [--image=ubuntu-24.04] [--dry-run]"
    exit 2
fi

# tenant-name regex must match SPEC-001 _TENANT_NAME_RE: ^[a-z][a-z0-9-]*$
if ! [[ "$TENANT_NAME" =~ ^[a-z][a-z0-9-]*$ ]]; then
    error "invalid tenant-name '$TENANT_NAME'"
    error "must match regex ^[a-z][a-z0-9-]*\$ (lowercase letter start, lowercase + digits + hyphens)"
    exit 2
fi

# ─── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_REPO="${BUBBLE_DATA_REPO:-$(cd "$PLATFORM_ROOT/.." && pwd)/bubble-vps-data}"

if [[ ! -d "$DATA_REPO" ]]; then
    error "data repo not found: $DATA_REPO"
    error "set BUBBLE_DATA_REPO env var or create the directory first"
    exit 2
fi

TENANT_DIR="$DATA_REPO/tenants/$TENANT_NAME"
YAML_PATH="$TENANT_DIR/tenant.yaml"

# Validate tenant scaffolding exists (operator must have run new-tenant.sh first).
if [[ ! -f "$YAML_PATH" ]]; then
    error "tenant scaffolding not found: $YAML_PATH"
    error "run scripts/new-tenant.sh $TENANT_NAME first"
    exit 2
fi

# ─── Required tools ───────────────────────────────────────────────────────
for tool in hcloud jq ssh python3 security; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        error "$tool not found in PATH"
        exit 1
    fi
done

# ─── Read HCLOUD_TOKEN from macOS Keychain ────────────────────────────────
# CRITICAL (SPEC-008 hard rule): the token MUST stay in a shell variable and
# be passed to hcloud only via env var (HCLOUD_TOKEN — natively read by hcloud).
# Never echo, log, or include in command-line args. Unset at end.
HCLOUD_TOKEN=$(security find-generic-password -s "hetzner-cloud" -a api_token -w 2>/dev/null || true)
if [[ -z "$HCLOUD_TOKEN" ]]; then
    error "no Hetzner Cloud token found in macOS Keychain"
    error "expected: service='hetzner-cloud', account='api_token'"
    error "fix: security add-generic-password -s 'hetzner-cloud' -a api_token -w '<token>'"
    exit 2
fi
export HCLOUD_TOKEN

# Cleanup hook — wipe the token from env on any exit path.
cleanup() { unset HCLOUD_TOKEN || true; }
trap cleanup EXIT

# ─── Discover SSH key + firewall in Hetzner ───────────────────────────────
heading "Discovering Hetzner resources"

# SSH key: default first match for SSH_KEY_FILTER (case-insensitive).
SSH_KEY_LINE=$(hcloud ssh-key list -o noheader -o columns=id,name | grep -i "$SSH_KEY_FILTER" | head -1 || true)
if [[ -z "$SSH_KEY_LINE" ]]; then
    error "no Hetzner SSH key matching '$SSH_KEY_FILTER' (case-insensitive)"
    error "run: hcloud ssh-key list   to see available keys"
    error "or pass --ssh-key=<name-or-id> to override"
    exit 3
fi
SSH_KEY_ID=$(echo "$SSH_KEY_LINE" | awk '{print $1}')
SSH_KEY_NAME=$(echo "$SSH_KEY_LINE" | awk '{print $2}')
info "ssh key:        $SSH_KEY_NAME (id=$SSH_KEY_ID)"

# Firewall: bubble-default (already created during Hetzner migration project).
FIREWALL_LINE=$(hcloud firewall list -o noheader -o columns=id,name | grep "bubble-default" | head -1 || true)
if [[ -z "$FIREWALL_LINE" ]]; then
    error "Hetzner firewall 'bubble-default' not found"
    error "run: hcloud firewall list   to see available firewalls"
    error "the firewall must already exist (created during Hetzner migration project)"
    exit 3
fi
FIREWALL_ID=$(echo "$FIREWALL_LINE" | awk '{print $1}')
info "firewall:       bubble-default (id=$FIREWALL_ID)"

SERVER_NAME="${TENANT_NAME}-vps"

# Show plan.
heading "Provisioning plan"
info "tenant:         $TENANT_NAME"
info "server name:    $SERVER_NAME"
info "type:           $SERVER_TYPE"
info "image:          $IMAGE"
info "region:         $REGION"
info "ssh key:        $SSH_KEY_NAME (id=$SSH_KEY_ID)"
info "firewall:       bubble-default (id=$FIREWALL_ID)"
info "tenant.yaml:    $YAML_PATH"

# ─── Dry-run short-circuit ────────────────────────────────────────────────
if [[ "$DRY_RUN" -eq 1 ]]; then
    warn "--dry-run set: skipping all hcloud server create / SSH / yaml-edit operations"
    info "exiting cleanly"
    exit 0
fi

# ─── Idempotency check: refuse to clobber existing server ────────────────
EXISTING=$(hcloud server list -o noheader -o columns=id,name | awk -v n="$SERVER_NAME" '$2 == n {print $1}' || true)
if [[ -n "$EXISTING" ]]; then
    error "Hetzner server '$SERVER_NAME' already exists (id=$EXISTING)"
    error "for v1 we refuse-on-clash. Future: --force flag will support recreation."
    error "manual workaround: hcloud server delete $SERVER_NAME   then re-run this script"
    exit 4
fi

# ─── Provision the server ────────────────────────────────────────────────
heading "Creating Hetzner server"

# Capture exit code explicitly — `set -e` doesn't always catch failures inside
# command substitution, especially when the failure is in a tool we then parse.
CREATE_EXIT=0
hcloud server create \
    --type "$SERVER_TYPE" \
    --image "$IMAGE" \
    --location "$REGION" \
    --name "$SERVER_NAME" \
    --ssh-key "$SSH_KEY_ID" \
    --firewall "$FIREWALL_ID" \
    --label "tenant=$TENANT_NAME" \
    --label "managed-by=bubble-vps-platform" || CREATE_EXIT=$?

if [[ "$CREATE_EXIT" -ne 0 ]]; then
    error "hcloud server create failed (exit $CREATE_EXIT)"
    error "check Hetzner Cloud Console for partially-created server: https://console.hetzner.cloud"
    exit 5
fi

info "server created"

# ─── Capture server ID + IP ───────────────────────────────────────────────
# Verify the server exists post-create (defensive — `set -e` would catch most
# but not pipeline-tail failures). Use `hcloud server describe -o json` for
# robust parsing.
SERVER_JSON=$(hcloud server describe "$SERVER_NAME" -o json)
SERVER_ID=$(echo "$SERVER_JSON" | jq -r '.id')
SERVER_IP=$(echo "$SERVER_JSON" | jq -r '.public_net.ipv4.ip')

if [[ -z "$SERVER_ID" || "$SERVER_ID" == "null" ]]; then
    error "could not capture server ID from hcloud describe"
    exit 5
fi
if [[ -z "$SERVER_IP" || "$SERVER_IP" == "null" ]]; then
    error "could not capture server IPv4 from hcloud describe"
    exit 5
fi

info "server id:      $SERVER_ID"
info "server ip:      $SERVER_IP"

# ─── Wait for SSH to become reachable ─────────────────────────────────────
heading "Waiting for SSH (max 5 min)"

# Poll every 10 seconds (NOT a tight loop — UFW LIMIT IN protects against
# floods on the box side too). Max 30 attempts = 5 minutes.
SSH_READY=0
for attempt in $(seq 1 30); do
    if ssh -o ConnectTimeout=5 \
           -o StrictHostKeyChecking=accept-new \
           -o UserKnownHostsFile=/dev/null \
           -o LogLevel=ERROR \
           "root@$SERVER_IP" 'echo ready' 2>/dev/null | grep -q '^ready$'; then
        SSH_READY=1
        info "SSH reachable on attempt $attempt"
        break
    fi
    sleep 10
done

if [[ "$SSH_READY" -ne 1 ]]; then
    error "SSH not reachable after 5 min (server may have failed to boot)"
    error "check: hcloud server describe $SERVER_NAME"
    error "the server EXISTS in Hetzner — manually delete or debug if not bootable"
    exit 6
fi

# ─── Bootstrap the non-root claude user ───────────────────────────────────
heading "Bootstrapping claude user (one-time)"

# Inline script as root: create claude, NOPASSWD sudo, copy SSH authorized_keys
# from /root/.ssh/. The `useradd ... || true` makes it idempotent across reruns
# (e.g. after a previous bootstrap that failed mid-way).
BOOTSTRAP_EXIT=0
ssh -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "root@$SERVER_IP" \
    'set -euo pipefail
     useradd -m -s /bin/bash claude || true
     usermod -aG sudo claude
     echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude
     chmod 0440 /etc/sudoers.d/claude
     mkdir -p /home/claude/.ssh
     cp /root/.ssh/authorized_keys /home/claude/.ssh/
     chown -R claude:claude /home/claude/.ssh
     chmod 700 /home/claude/.ssh
     chmod 600 /home/claude/.ssh/authorized_keys' || BOOTSTRAP_EXIT=$?

if [[ "$BOOTSTRAP_EXIT" -ne 0 ]]; then
    error "claude-user bootstrap failed (ssh exit $BOOTSTRAP_EXIT)"
    error "the server EXISTS in Hetzner — fix manually then run pyinfra"
    exit 7
fi
info "claude user created (NOPASSWD sudo, authorized_keys copied)"

# Verify post-bootstrap: SSH as claude must work.
VERIFY_EXIT=0
ssh -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "claude@$SERVER_IP" 'sudo -n echo verified' >/dev/null 2>&1 || VERIFY_EXIT=$?

if [[ "$VERIFY_EXIT" -ne 0 ]]; then
    warn "claude-user post-bootstrap verification failed (ssh exit $VERIFY_EXIT)"
    warn "this is non-fatal but pyinfra deploy may fail; investigate before running deploy"
fi

# ─── Update tenant.yaml in-place ──────────────────────────────────────────
heading "Updating tenant.yaml with real values"

# IMPORTANT: env-var-prefix form (VAR=val cmd) MUST come BEFORE the executable;
# bash treats it as positional args otherwise (regression-test rule).
__SERVER_IP__="$SERVER_IP" \
__SERVER_ID__="$SERVER_ID" \
__TENANT_NAME__="$TENANT_NAME" \
__YAML_PATH__="$YAML_PATH" \
__REGION__="$REGION" \
python3 <<'PYEOF'
import os
from pathlib import Path
import yaml

path = Path(os.environ['__YAML_PATH__'])
data = yaml.safe_load(path.read_text())

# Hetzner location codes: fsn1 → fsn1-dc14 (data center). For other regions
# the operator can edit afterward; we just default to the canonical dc.
region_map = {
    'fsn1': 'fsn1-dc14',
    'nbg1': 'nbg1-dc3',
    'hel1': 'hel1-dc2',
    'ash':  'ash-dc1',
    'hil':  'hil-dc1',
}
region_value = region_map.get(os.environ['__REGION__'], os.environ['__REGION__'])

data.setdefault('host', {})
data['host']['ip'] = os.environ['__SERVER_IP__']
data['host']['hostname'] = f"{os.environ['__TENANT_NAME__']}-vps"
data['host']['provider_server_id'] = os.environ['__SERVER_ID__']
data['host']['region'] = region_value

path.write_text(yaml.safe_dump(data, sort_keys=False))
PYEOF

info "tenant.yaml updated"

# ─── Re-validate via tenant_loader ────────────────────────────────────────
# Defense in depth: if the box exists but tenant.yaml is broken, surface it now
# rather than letting pyinfra deploy fail with a confusing error.
VALIDATE_EXIT=0
__YAML_PATH__="$YAML_PATH" \
__TENANT_NAME__="$TENANT_NAME" \
__PLATFORM_ROOT__="$PLATFORM_ROOT" \
python3 <<'PYEOF' || VALIDATE_EXIT=$?
import os, sys
sys.path.insert(0, os.environ['__PLATFORM_ROOT__'])
from lib.tenant_loader import load_tenant_from_path, TenantConfigError
try:
    cfg = load_tenant_from_path(
        os.environ['__YAML_PATH__'],
        expected_name=os.environ['__TENANT_NAME__'],
    )
    # Confirm host.ip is no longer a placeholder.
    if 'PLACEHOLDER' in str(cfg.host.ip):
        print(f"ERROR: host.ip still a placeholder: {cfg.host.ip}", file=sys.stderr)
        sys.exit(2)
    print(f"validated: host.ip={cfg.host.ip} hostname={cfg.host.hostname}")
except TenantConfigError as e:
    # Operator may not have filled in contact.primary_email or telegram_user_id
    # yet — that's OK at this stage of provisioning, surface as warning not error.
    msg = str(e)
    if 'contact' in msg.lower() or 'telegram' in msg.lower() or 'email' in msg.lower():
        print(f"WARN: tenant.yaml has placeholder contact fields (fill manually): {e}", file=sys.stderr)
        sys.exit(0)  # non-fatal
    print(f"ERROR: tenant.yaml validation failed: {e}", file=sys.stderr)
    sys.exit(2)
PYEOF

if [[ "$VALIDATE_EXIT" -ne 0 ]]; then
    error "tenant.yaml post-update validation failed (exit $VALIDATE_EXIT)"
    error "the box EXISTS — fix tenant.yaml manually before pyinfra deploy"
    exit 8
fi

info "tenant.yaml re-validates via lib.tenant_loader"

# ─── Friendly success summary ─────────────────────────────────────────────
heading "Done — server provisioned"

cat <<EOF

Tenant ${BOLD}$TENANT_NAME${NC} provisioned at:
    server name:  $SERVER_NAME
    server id:    $SERVER_ID
    server ip:    $SERVER_IP
    region:       $REGION

${BOLD}Next steps:${NC}
  1. Fill remaining placeholders in ${YELLOW}$YAML_PATH${NC}
     (contact.primary_email, contact.primary_telegram_user_id)

  2. Paste real secret values via:
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=TELEGRAM_BOT_TOKEN
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=CLAUDE_CODE_OAUTH_TOKEN
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=TAILSCALE_AUTHKEY
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=PHONEHOME_TOKEN
     (Generate PHONEHOME_TOKEN with: openssl rand -hex 32)

  3. Bootstrap operator master age key (one-time, if not done already):
       scripts/operator-bootstrap-age.sh

  4. Deploy:
       cd $PLATFORM_ROOT
       ./scripts/deploy.sh --tenant=$TENANT_NAME
     ${DIM}First deploy will halt at the box-pubkey-bootstrap gate (Phase D);
     operator manually adds box pubkey to .sops.yaml + runs sops updatekeys,
     then re-deploys.${NC}

EOF
