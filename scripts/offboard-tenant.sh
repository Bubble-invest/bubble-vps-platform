#!/usr/bin/env bash
# Offboard a tenant — close the lifecycle loop.
#
# Per SPEC-018 (Step 7c): two modes.
#
#   --mode=handoff (default)  Remove our access; box keeps running for client.
#                             - Removes operator master pubkey from this tenant's
#                               .sops.yaml rule (box pubkey stays so box can decrypt)
#                             - sops updatekeys --yes (re-encrypt with smaller list)
#                             - Prints manual Tailscale removal instructions
#                             - Archives tenants/<name>/ to tenants/_archive/
#                             - Prints client handoff doc to stdout
#
#   --mode=destroy            Provably destroy the Hetzner server.
#                             - hcloud server delete <id>
#                             - Prints manual Tailscale removal instructions
#                             - Archives tenants/<name>/ to tenants/_archive/
#                             - Requires TWO prompts (yes + tenant-name typed)
#
# Usage:
#   ./scripts/offboard-tenant.sh <tenant-name> [--mode=handoff|destroy] [--yes]
#
# Examples:
#   ./scripts/offboard-tenant.sh acme-corp
#   ./scripts/offboard-tenant.sh acme-corp --mode=handoff
#   ./scripts/offboard-tenant.sh acme-corp --mode=destroy
#   ./scripts/offboard-tenant.sh acme-corp --mode=destroy --yes  # CI / scripted
#
# Requirements (handoff):
#   - sops + age installed (operator-bootstrap-age.sh handles install)
#   - SOPS_AGE_KEY_FILE present (we need to decrypt + re-encrypt)
#   - python3 with pyyaml (the platform .venv has it)
#
# Requirements (destroy):
#   - hcloud CLI (brew install hcloud)
#   - HCLOUD_TOKEN in macOS Keychain: service="hetzner-cloud", account="api_token"
#
# Boilerplate part of bubble-vps-platform — works for any existing tenant.
#
# NOTE on --yes in destroy mode: --yes skips ONE prompt only — the typed-name
# prompt is the second safety net and DOES still fire (defends against muscle-
# memory typos that pair --yes with the wrong tenant name).

set -uo pipefail

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
info()    { printf "${GREEN}OK${NC} %s\n" "$*"; }
warn()    { printf "${YELLOW}!${NC} %s\n" "$*"; }
error()   { printf "${RED}X${NC} %s\n" "$*" >&2; }
heading() { printf "\n${BOLD}%s${NC}\n" "$*"; }

# ─── Argument parsing ──────────────────────────────────────────────────────
TENANT_NAME=""
MODE="handoff"
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode=*)  MODE="${1#--mode=}"; shift ;;
        --yes|-y)  ASSUME_YES=1; shift ;;
        --help|-h)
            grep '^#' "$0" | head -40 | sed 's/^# //;s/^#//'
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
    error "usage: $0 <tenant-name> [--mode=handoff|destroy] [--yes]"
    exit 2
fi

# tenant-name regex must match SPEC-001 _TENANT_NAME_RE: ^[a-z][a-z0-9-]*$
if ! [[ "$TENANT_NAME" =~ ^[a-z][a-z0-9-]*$ ]]; then
    error "invalid tenant-name '$TENANT_NAME'"
    error "must match regex ^[a-z][a-z0-9-]*\$ (lowercase letter start, lowercase + digits + hyphens)"
    exit 2
fi

if [[ "$MODE" != "handoff" && "$MODE" != "destroy" ]]; then
    error "invalid --mode '$MODE' — must be 'handoff' or 'destroy'"
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
SOPS_YAML="$DATA_REPO/.sops.yaml"
ARCHIVE_BASE="$DATA_REPO/tenants/_archive"

# Validate tenant exists.
if [[ ! -f "$YAML_PATH" ]]; then
    error "tenant not found: $YAML_PATH"
    if [[ -d "$ARCHIVE_BASE" ]]; then
        # Helpful hint: maybe already offboarded?
        for archived in "$ARCHIVE_BASE/${TENANT_NAME}"-*; do
            if [[ -d "$archived" ]]; then
                error "did you mean an already-archived tenant? Found: $archived"
                break
            fi
        done
    fi
    exit 2
fi

# Validate .sops.yaml exists (needed for handoff; informational for destroy).
if [[ ! -f "$SOPS_YAML" ]]; then
    error ".sops.yaml not found in data repo: $SOPS_YAML"
    error "this is required for the handoff mode (re-encrypt with smaller recipient list)"
    exit 2
fi

# ─── Required tools ───────────────────────────────────────────────────────
for tool in python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        error "$tool not found in PATH"
        exit 1
    fi
done

# Prefer the platform venv if available — it has pyyaml.
PYTHON_BIN="python3"
if [[ -x "$PLATFORM_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$PLATFORM_ROOT/.venv/bin/python"
fi
if ! "$PYTHON_BIN" -c "import yaml" >/dev/null 2>&1; then
    error "python3 has no 'yaml' module ($PYTHON_BIN)"
    error "install with: pip install pyyaml (or activate the platform .venv)"
    exit 1
fi

if [[ "$MODE" == "handoff" ]]; then
    if ! command -v sops >/dev/null 2>&1; then
        error "sops not installed (handoff needs sops updatekeys). Run scripts/operator-bootstrap-age.sh"
        exit 1
    fi
    export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
    if [[ ! -f "$SOPS_AGE_KEY_FILE" ]]; then
        error "age key file not found: $SOPS_AGE_KEY_FILE"
        error "needed to re-encrypt secrets file with the smaller recipient list"
        exit 1
    fi
elif [[ "$MODE" == "destroy" ]]; then
    for tool in hcloud security; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            error "$tool not found in PATH (destroy mode needs hcloud + security)"
            exit 1
        fi
    done
fi

# ─── Read tenant info (informational; never echoed for secrets) ───────────
# These are infrastructure metadata, not secrets — safe to display.
TENANT_HOSTNAME=$(__YAML__="$YAML_PATH" "$PYTHON_BIN" -c "
import os, yaml
data = yaml.safe_load(open(os.environ['__YAML__']).read())
print((data.get('host') or {}).get('hostname', '<unknown>'))
")
TENANT_IP=$(__YAML__="$YAML_PATH" "$PYTHON_BIN" -c "
import os, yaml
data = yaml.safe_load(open(os.environ['__YAML__']).read())
print((data.get('host') or {}).get('ip', '<unknown>'))
")
TENANT_SERVER_ID=$(__YAML__="$YAML_PATH" "$PYTHON_BIN" -c "
import os, yaml
data = yaml.safe_load(open(os.environ['__YAML__']).read())
print((data.get('host') or {}).get('provider_server_id', '<unknown>'))
")

# ─── Big warning banner ──────────────────────────────────────────────────
heading "============================================================"
if [[ "$MODE" == "handoff" ]]; then
    heading "  TENANT OFFBOARDING — HANDOFF MODE"
else
    heading "  TENANT OFFBOARDING — DESTROY MODE (IRREVERSIBLE)"
fi
heading "============================================================"

cat <<EOF

  Tenant:        ${BOLD}${TENANT_NAME}${NC}
  Hostname:      ${TENANT_HOSTNAME}
  Public IP:     ${TENANT_IP}
  Server ID:     ${TENANT_SERVER_ID}

EOF

if [[ "$MODE" == "handoff" ]]; then
    cat <<EOF
  ${BOLD}Actions${NC} (handoff — box keeps running for the client):
    1. Remove operator master pubkey from this tenant's .sops.yaml rule
       (box pubkey stays — the box can still decrypt its own secrets)
    2. Run sops updatekeys to re-encrypt with the smaller recipient list
       (after this we can NO LONGER decrypt the tenant's secrets)
    3. Print manual Tailscale removal instructions
    4. Archive tenants/${TENANT_NAME}/ -> tenants/_archive/${TENANT_NAME}-handoff-<date>/
    5. Print client handoff doc

EOF
else
    cat <<EOF
  ${BOLD}Actions${NC} (destroy — server WILL be deleted from Hetzner):
    1. Read HCLOUD_TOKEN from macOS Keychain
    2. ${RED}DELETE${NC} Hetzner server id=${TENANT_SERVER_ID} (PERMANENT)
    3. Print manual Tailscale removal instructions
    4. Archive tenants/${TENANT_NAME}/ -> tenants/_archive/${TENANT_NAME}-destroyed-<date>/
    5. Print destruction confirmation

  ${RED}${BOLD}THIS DELETES DATA. THERE IS NO UNDO.${NC}
  ${DIM}(Hetzner snapshots are separate; we don't auto-snapshot here.)${NC}

EOF
fi

# ─── First confirmation: must type "yes" exactly (unless --yes) ──────────
if [[ "$ASSUME_YES" -ne 1 ]]; then
    printf 'Type "yes" to proceed (anything else aborts): '
    CONFIRM=""
    read -r CONFIRM
    if [[ "$CONFIRM" != "yes" ]]; then
        warn "aborted (input was '$CONFIRM', not exactly 'yes')"
        exit 0
    fi
fi

# ─── Second confirmation (destroy only): must type tenant name exactly ───
# IMPORTANT: this is the second safety net for destroy mode. It DOES fire
# even when --yes is passed (per spec: defends against `--yes` + wrong name
# muscle-memory typo). Operator can still bypass via input from a here-string,
# but that's a deliberate choice they have to make.
if [[ "$MODE" == "destroy" && "$ASSUME_YES" -ne 1 ]]; then
    printf "Type the tenant name '%s' to confirm DESTRUCTION: " "$TENANT_NAME"
    NAME_CONFIRM=""
    read -r NAME_CONFIRM
    if [[ "$NAME_CONFIRM" != "$TENANT_NAME" ]]; then
        warn "aborted (typed '$NAME_CONFIRM', expected '$TENANT_NAME')"
        exit 0
    fi
fi

# ─── HANDOFF MODE ────────────────────────────────────────────────────────
if [[ "$MODE" == "handoff" ]]; then
    heading "Step 1: edit .sops.yaml (remove operator master from tenant rule)"

    # The .sops.yaml editing is delicate. We need to:
    #   1. Find the rule whose path_regex is specific to THIS tenant
    #      (i.e. path_regex contains the tenant name, e.g.
    #       'tenants/<name>/secrets\.sops\.env$')
    #   2. Remove the operator master pubkey from its `age:` recipient list
    #   3. KEEP the box pubkey (so the box continues to decrypt for itself)
    #   4. If no specific rule exists (only the catch-all matches), exit-fail
    #      with a clear message — removing operator master from the catch-all
    #      would leave NO recipient (unencryptable file).
    #
    # We use pyyaml. Comment preservation isn't perfect (pyyaml drops them on
    # round-trip), but the spec's idempotency rules say nice-to-have. The
    # essential structure (creation_rules, path_regex, age) is preserved.

    OPERATOR_MASTER="age1qal34hv5h99vvpq7kmghfz0mjh98eq9mj5dg5k43r8kwmumvnu5qt6w3hy"

    EDIT_EXIT=0
    __SOPS_YAML__="$SOPS_YAML" \
    __TENANT__="$TENANT_NAME" \
    __OPERATOR__="$OPERATOR_MASTER" \
    "$PYTHON_BIN" <<'PYEOF' || EDIT_EXIT=$?
import os
import sys
import yaml

sops_yaml = os.environ['__SOPS_YAML__']
tenant = os.environ['__TENANT__']
operator = os.environ['__OPERATOR__']

with open(sops_yaml, 'r', encoding='utf-8') as fh:
    data = yaml.safe_load(fh)

rules = data.get('creation_rules') or []

# Find the rule that's SPECIFIC to this tenant (not the catch-all).
# A specific rule's path_regex contains the tenant name as a path segment.
specific_idx = None
for i, rule in enumerate(rules):
    pr = rule.get('path_regex', '') or ''
    # Match patterns like: tenants/<name>/secrets\.sops\.env$
    if f"tenants/{tenant}/" in pr:
        specific_idx = i
        break

if specific_idx is None:
    print(f"ERROR: tenant '{tenant}' has no specific .sops.yaml rule.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Handoff requires a tenant-specific rule with TWO recipients:", file=sys.stderr)
    print("  - operator master pubkey (will be removed)", file=sys.stderr)
    print("  - per-tenant box pubkey  (must remain — box decrypts its own secrets)", file=sys.stderr)
    print("", file=sys.stderr)
    print("If only the catch-all matches, removing operator master would leave", file=sys.stderr)
    print("the file with ZERO recipients (unencryptable). Add a tenant-specific", file=sys.stderr)
    print("rule first (typically done at Phase D first-half deploy time when the", file=sys.stderr)
    print("box generates its own age key), then re-run.", file=sys.stderr)
    sys.exit(3)

rule = rules[specific_idx]
age_field = rule.get('age', '')

# `age` is a comma-separated string (sometimes multi-line via YAML's >- folded
# scalar). Split on commas, strip whitespace + newlines, drop empties.
recipients = [r.strip() for r in age_field.replace('\n', ' ').split(',')]
recipients = [r for r in recipients if r]

if operator not in recipients:
    print(f"WARN: operator master pubkey not in tenant '{tenant}' rule.", file=sys.stderr)
    print(f"      Tenant may already be handed off. Recipients: {recipients}", file=sys.stderr)
    # Idempotent — not an error. Box can still decrypt; nothing to remove.
    sys.exit(0)

new_recipients = [r for r in recipients if r != operator]

if not new_recipients:
    print(f"ERROR: removing operator master from tenant '{tenant}' would leave", file=sys.stderr)
    print(f"       ZERO recipients (file would become unencryptable).", file=sys.stderr)
    print(f"       Add a per-tenant box pubkey to the rule first.", file=sys.stderr)
    sys.exit(4)

# Reconstruct as a single-string comma-separated value (pyyaml will dump it as
# a single line; folded scalar is for readability, not semantics).
rule['age'] = ', '.join(new_recipients)
rules[specific_idx] = rule
data['creation_rules'] = rules

# Round-trip via pyyaml. Comments will be lost; structural validity preserved.
with open(sops_yaml, 'w', encoding='utf-8') as fh:
    yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)

print(f"removed operator master from rule {specific_idx} for tenant '{tenant}'")
print(f"remaining recipients: {new_recipients}")
PYEOF

    if [[ "$EDIT_EXIT" -ne 0 ]]; then
        error ".sops.yaml edit failed (exit $EDIT_EXIT)"
        error "no changes have been made (sops updatekeys NOT run)"
        exit 5
    fi

    # Verify we didn't corrupt YAML by re-loading.
    VERIFY_EXIT=0
    __SOPS_YAML__="$SOPS_YAML" "$PYTHON_BIN" -c "
import os, yaml, sys
try:
    data = yaml.safe_load(open(os.environ['__SOPS_YAML__']).read())
    if not isinstance(data, dict) or 'creation_rules' not in data:
        sys.exit(2)
    print(f'verified: {len(data[\"creation_rules\"])} creation_rules in .sops.yaml')
except Exception as e:
    print(f'YAML re-load failed: {e}', file=sys.stderr)
    sys.exit(3)
" || VERIFY_EXIT=$?

    if [[ "$VERIFY_EXIT" -ne 0 ]]; then
        error ".sops.yaml became unparseable after edit (exit $VERIFY_EXIT)"
        error "MANUAL FIX REQUIRED — restore from git: cd $DATA_REPO && git checkout .sops.yaml"
        exit 5
    fi

    info ".sops.yaml updated"

    heading "Step 2: sops updatekeys (re-encrypt with smaller recipient list)"

    # Run sops updatekeys to actually re-encrypt the file with the new (smaller)
    # recipient list. After this, the operator can NO LONGER decrypt the tenant's
    # secrets. Capture exit code explicitly per Step 7a's lesson — `set -e` does
    # NOT propagate from a subshell in compound statements.
    SOPS_EXIT=0
    ( cd "$DATA_REPO" && sops updatekeys --yes "tenants/$TENANT_NAME/secrets.sops.env" ) || SOPS_EXIT=$?

    if [[ "$SOPS_EXIT" -ne 0 ]]; then
        error "sops updatekeys failed (exit $SOPS_EXIT)"
        error ".sops.yaml HAS BEEN edited but secrets.sops.env was NOT re-keyed"
        error "MANUAL FIX: cd $DATA_REPO && sops updatekeys tenants/$TENANT_NAME/secrets.sops.env"
        exit 6
    fi

    info "secrets.sops.env re-encrypted with smaller recipient list"

# ─── DESTROY MODE ─────────────────────────────────────────────────────────
elif [[ "$MODE" == "destroy" ]]; then
    heading "Step 1: read HCLOUD_TOKEN from Keychain"

    # CRITICAL (SPEC-008 hard rule): the token MUST stay in a shell variable and
    # be passed to hcloud only via env var (HCLOUD_TOKEN — natively read by hcloud).
    # Never echo, log, or include in command-line args. Unset on every exit path.
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

    info "HCLOUD_TOKEN loaded into env (will be unset on exit)"

    heading "Step 2: hcloud server delete"

    if [[ -z "$TENANT_SERVER_ID" || "$TENANT_SERVER_ID" == "<unknown>" || "$TENANT_SERVER_ID" == *PLACEHOLDER* ]]; then
        warn "tenant.yaml has no usable provider_server_id ($TENANT_SERVER_ID)"
        warn "skipping hcloud server delete — operator must clean up manually"
        warn "see Hetzner console: https://console.hetzner.cloud"
    else
        # Capture exit code explicitly. Per Step 7a's lesson, `set -e` does NOT
        # propagate from compound statements / subshells reliably.
        DELETE_EXIT=0
        hcloud server delete "$TENANT_SERVER_ID" || DELETE_EXIT=$?

        if [[ "$DELETE_EXIT" -ne 0 ]]; then
            # Spec idempotency: if the server is already gone, hcloud returns
            # 404 / non-zero. Log + continue (the goal is achieved).
            warn "hcloud server delete exited $DELETE_EXIT — server may already be gone"
            warn "verify manually: hcloud server list"
        else
            info "Hetzner server $TENANT_SERVER_ID deleted"
        fi
    fi
fi

# ─── Manual step banner (both modes) ─────────────────────────────────────
heading "MANUAL STEP REQUIRED: remove from Tailscale tailnet"

cat <<EOF
  ${YELLOW}Open${NC} https://login.tailscale.com/admin/machines
  ${YELLOW}Find${NC}  ${TENANT_HOSTNAME}  (or its IP ${TENANT_IP})
  ${YELLOW}Click${NC} 'Remove device' (three-dot menu)

  ${DIM}Programmatic Tailscale device removal requires a Tailscale API token in
  the Keychain — deferred to v2 when we automate at-scale.${NC}

EOF

# ─── Archive tenant directory (both modes) ───────────────────────────────
heading "Archive tenant directory"

DATE=$(date -u +%Y-%m-%d)
mkdir -p "$ARCHIVE_BASE"

if [[ "$MODE" == "handoff" ]]; then
    ARCHIVE_DIR="$ARCHIVE_BASE/${TENANT_NAME}-handoff-${DATE}"
else
    ARCHIVE_DIR="$ARCHIVE_BASE/${TENANT_NAME}-destroyed-${DATE}"
fi

# Idempotency guard: if the archive path already exists (offboarded twice in
# one day), append a counter to avoid clobbering the prior archive.
if [[ -e "$ARCHIVE_DIR" ]]; then
    counter=2
    while [[ -e "${ARCHIVE_DIR}-${counter}" ]]; do
        counter=$((counter + 1))
    done
    ARCHIVE_DIR="${ARCHIVE_DIR}-${counter}"
    warn "archive path already existed; using $ARCHIVE_DIR instead"
fi

mv "$TENANT_DIR" "$ARCHIVE_DIR"
info "archived: $TENANT_DIR -> $ARCHIVE_DIR"

# ─── Final summary ───────────────────────────────────────────────────────
if [[ "$MODE" == "handoff" ]]; then
    heading "Done — tenant handed off"

    cat <<EOF

  Tenant ${BOLD}${TENANT_NAME}${NC} has been handed off.
    - Operator master removed from .sops.yaml rule
    - secrets.sops.env re-encrypted with box pubkey only
    - Tenant dir archived at: ${ARCHIVE_DIR}

  ${BOLD}STILL TODO MANUALLY${NC}:
    1. Remove the box from Tailscale (instructions above)
    2. Email the client the handoff doc below
    3. Commit + push the .sops.yaml + archive in the data repo

EOF

    cat <<EOF
${BOLD}─────────────── CLIENT HANDOFF DOC ───────────────${NC}

Dear client,

Your Bubble VPS is now operating standalone. As of ${DATE}, Bubble Invest no
longer has access to:
   - SSH (Tailscale device removed from our tailnet)
   - Secrets (our master age key removed from your SOPS recipients)
   - Configuration changes (we cannot deploy updates from our side)

Your box continues to run normally. Your data and secrets remain intact and
encrypted with your box's age key (located on the box at /etc/age/key.txt
- back it up to a secure location).

Going forward:
   - You retain full root access via your SSH key (the one you provided)
   - Your secrets are decryptable only with /etc/age/key.txt - back it up
     (USB key, password manager, etc.)
   - Updates: clone the open-source bubble-vps-platform repo, run pyinfra
     deploy yourself
   - Support: 30 days transition support included - reply to this email

Best,
Bubble Invest

${BOLD}──────────────────────────────────────────────────${NC}

EOF
else
    heading "Done — tenant destroyed"

    cat <<EOF

  Tenant ${BOLD}${TENANT_NAME}${NC} has been destroyed.
    - Hetzner server id=${TENANT_SERVER_ID} deleted
    - Tenant dir archived at: ${ARCHIVE_DIR}

  ${BOLD}STILL TODO MANUALLY${NC}:
    1. Remove the box from Tailscale (instructions above)
    2. Send destruction confirmation to client (date=${DATE}, server=${TENANT_SERVER_ID})
    3. Commit + push the archive in the data repo

  ${DIM}Note: the .sops.yaml is unchanged — historical archive can still be
  decrypted by the operator master if needed for audit / forensics.${NC}

EOF
fi
