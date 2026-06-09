#!/usr/bin/env bash
# Scaffold a new tenant directory in bubble-vps-data.
#
# Per SPEC-016: gets the operator from "deal closed" to "ready to fill in
# tenant-specific values" in <30 seconds. Generates tenant.yaml, encrypted
# secrets.sops.env (with placeholder values), persona CLAUDE.md stubs, and
# README.md. Does NOT provision Hetzner boxes (Step 7b) and does NOT touch
# real secret values (operator pastes those via operator-set-secret.sh).
#
# Usage:
#   ./scripts/new-tenant.sh <tenant-name> [--type=client|internal] \
#       [--display-name="Acme Corp"] [--persona=morty] [--force]
#
# Examples:
#   ./scripts/new-tenant.sh acme-corp
#   ./scripts/new-tenant.sh acme-corp --type=client --display-name="Acme Corp"
#   ./scripts/new-tenant.sh acme-corp --persona=acme-bot --force
#
# Requirements:
#   - sops + age installed (operator-bootstrap-age.sh handles install)
#   - SOPS_AGE_KEY_FILE present (default ~/.config/sops/age/keys.txt)
#   - bubble-vps-data sibling repo (override via BUBBLE_DATA_REPO env)
#   - python3 with jinja2 (the platform .venv has it)
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
TENANT_TYPE="client"
DISPLAY_NAME=""
PERSONA_NAME=""
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --type=*)         TENANT_TYPE="${1#--type=}"; shift ;;
        --display-name=*) DISPLAY_NAME="${1#--display-name=}"; shift ;;
        --persona=*)      PERSONA_NAME="${1#--persona=}"; shift ;;
        --force)          FORCE=1; shift ;;
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
    error "usage: $0 <tenant-name> [--type=client|internal] [--display-name=...] [--persona=...] [--force]"
    exit 2
fi

# tenant-name regex must match SPEC-001 _TENANT_NAME_RE: ^[a-z][a-z0-9-]*$
if ! [[ "$TENANT_NAME" =~ ^[a-z][a-z0-9-]*$ ]]; then
    error "invalid tenant-name '$TENANT_NAME'"
    error "must match regex ^[a-z][a-z0-9-]*\$ (lowercase letter start, lowercase + digits + hyphens)"
    exit 2
fi

if [[ "$TENANT_TYPE" != "client" && "$TENANT_TYPE" != "internal" ]]; then
    error "invalid --type '$TENANT_TYPE' — must be 'client' or 'internal'"
    exit 2
fi

# Defaults
if [[ -z "$DISPLAY_NAME" ]]; then
    # Title-case the tenant-name (e.g. "acme-corp" → "Acme Corp").
    DISPLAY_NAME=$(__NAME__="$TENANT_NAME" python3 -c "
import os
name = os.environ['__NAME__']
print(' '.join(part.capitalize() for part in name.split('-')))
")
fi
if [[ -z "$PERSONA_NAME" ]]; then
    PERSONA_NAME="$TENANT_NAME"
fi

# Validate persona name matches the same regex (we use it as a dir name).
if ! [[ "$PERSONA_NAME" =~ ^[a-z][a-z0-9-]*$ ]]; then
    error "invalid --persona '$PERSONA_NAME' — must match ^[a-z][a-z0-9-]*\$"
    exit 2
fi

# ─── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_REPO="${BUBBLE_DATA_REPO:-$(cd "$PLATFORM_ROOT/.." && pwd)/bubble-vps-data}"
TEMPLATES_DIR="$PLATFORM_ROOT/pyinfra/templates"

if [[ ! -d "$DATA_REPO" ]]; then
    error "data repo not found: $DATA_REPO"
    error "set BUBBLE_DATA_REPO env var or create the directory first"
    exit 2
fi

TENANT_DIR="$DATA_REPO/tenants/$TENANT_NAME"

if [[ -e "$TENANT_DIR" ]]; then
    if [[ "$FORCE" -ne 1 ]]; then
        error "tenant directory already exists: $TENANT_DIR"
        error "use --force to overwrite (DESTRUCTIVE — re-encrypts secrets with placeholder values)"
        exit 2
    fi
    warn "overwriting existing tenant directory (--force)"
fi

# Verify python3 + jinja2 available (script depends on them for rendering).
if ! command -v python3 >/dev/null 2>&1; then
    error "python3 not found in PATH"
    exit 1
fi
# Prefer the platform venv if available — it definitely has jinja2.
PYTHON_BIN="python3"
if [[ -x "$PLATFORM_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$PLATFORM_ROOT/.venv/bin/python"
fi
if ! "$PYTHON_BIN" -c "import jinja2" >/dev/null 2>&1; then
    error "python3 has no 'jinja2' module ($PYTHON_BIN)"
    error "install with: pip install jinja2 (or activate the platform .venv)"
    exit 1
fi

if ! command -v sops >/dev/null 2>&1; then
    error "sops not installed. Run scripts/operator-bootstrap-age.sh first."
    exit 1
fi

export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
if [[ ! -f "$SOPS_AGE_KEY_FILE" ]]; then
    error "age key file not found: $SOPS_AGE_KEY_FILE"
    error "run scripts/operator-bootstrap-age.sh to generate one"
    exit 1
fi

# ─── Compute template variables ────────────────────────────────────────────
CREATED_AT=$(date -u +%Y-%m-%d)
PROVISIONED_BY="${USER:-unknown}"

heading "Scaffolding tenant: ${BOLD}$TENANT_NAME${NC}"
info "type:           $TENANT_TYPE"
info "display name:   $DISPLAY_NAME"
info "persona:        $PERSONA_NAME"
info "data repo:      $DATA_REPO"
info "created at:     $CREATED_AT (provisioned_by=$PROVISIONED_BY)"

# ─── 1. Create directory structure ─────────────────────────────────────────
heading "Step 1: directory structure"

# If --force and dir exists, wipe it first (clean slate). Operator already
# saw the warning above.
if [[ "$FORCE" -eq 1 && -e "$TENANT_DIR" ]]; then
    rm -rf "$TENANT_DIR"
fi

mkdir -p "$TENANT_DIR/persona/$PERSONA_NAME/workspace"
info "created $TENANT_DIR/persona/$PERSONA_NAME/workspace/"

# ─── 2-4. Render jinja2 templates ──────────────────────────────────────────
heading "Step 2: render templates"

# IMPORTANT: env-var-prefix form (VAR=val cmd) MUST come BEFORE the executable;
# bash treats it as positional args otherwise (regression-test rule).
render_template() {
    local template_file="$1"
    local output_file="$2"
    __TPL__="$template_file" __OUT__="$output_file" \
    __TENANT_NAME__="$TENANT_NAME" \
    __TENANT_TYPE__="$TENANT_TYPE" \
    __DISPLAY_NAME__="$DISPLAY_NAME" \
    __PERSONA_NAME__="$PERSONA_NAME" \
    __CREATED_AT__="$CREATED_AT" \
    __PROVISIONED_BY__="$PROVISIONED_BY" \
    "$PYTHON_BIN" -c "
import os
from jinja2 import Environment, FileSystemLoader, StrictUndefined

tpl_path = os.environ['__TPL__']
out_path = os.environ['__OUT__']
tpl_dir = os.path.dirname(tpl_path)
tpl_name = os.path.basename(tpl_path)

env = Environment(
    loader=FileSystemLoader(tpl_dir),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
tpl = env.get_template(tpl_name)
rendered = tpl.render(
    tenant_name=os.environ['__TENANT_NAME__'],
    tenant_type=os.environ['__TENANT_TYPE__'],
    display_name=os.environ['__DISPLAY_NAME__'],
    persona_name=os.environ['__PERSONA_NAME__'],
    created_at=os.environ['__CREATED_AT__'],
    provisioned_by=os.environ['__PROVISIONED_BY__'],
)
with open(out_path, 'w', encoding='utf-8') as fh:
    fh.write(rendered)
"
}

render_template "$TEMPLATES_DIR/tenant.yaml.j2"               "$TENANT_DIR/tenant.yaml"
info "wrote tenant.yaml"

render_template "$TEMPLATES_DIR/tenant-readme.md.j2"          "$TENANT_DIR/README.md"
info "wrote README.md"

render_template "$TEMPLATES_DIR/persona-claude-md.j2"         "$TENANT_DIR/persona/$PERSONA_NAME/CLAUDE.md"
info "wrote persona/$PERSONA_NAME/CLAUDE.md"

render_template "$TEMPLATES_DIR/persona-workspace-claude-md.j2" "$TENANT_DIR/persona/$PERSONA_NAME/workspace/CLAUDE.md"
info "wrote persona/$PERSONA_NAME/workspace/CLAUDE.md"

# ─── 5. Create + encrypt initial secrets.sops.env ──────────────────────────
heading "Step 3: encrypted secrets file"

SECRETS_FILE="$TENANT_DIR/secrets.sops.env"

# Write plaintext placeholder file at the TARGET path. SOPS uses the file path
# (relative to .sops.yaml's containing dir) for path_regex matching → must be
# the real target path, not a /tmp file. Values are literal placeholder
# strings — no SPEC-008 leak risk because they don't contain real secrets.
cat > "$SECRETS_FILE" <<'EOF'
TELEGRAM_BOT_TOKEN=PASTE_FROM_BOTFATHER
CLAUDE_CODE_OAUTH_TOKEN=PASTE_FROM_CLAUDE_SETUP_TOKEN
TAILSCALE_AUTHKEY=PASTE_FROM_TAILSCALE_ADMIN
PHONEHOME_TOKEN=GENERATE_VIA_OPENSSL_RAND_HEX_32
EOF

info "wrote plaintext placeholder secrets.sops.env"

# IMPORTANT: sops walks UP from the current working directory to find
# .sops.yaml. So we must cd into the data repo before invoking sops.
# (Same pattern used by operator-set-secret.sh — well-trodden path.)
#
# CRITICAL safety check: if .sops.yaml is missing in the data repo, sops fails
# silently leaving the secrets file PLAINTEXT on disk. Catch this before
# anything writes secrets and exit-fail the script. Bug discovered 2026-05-09
# during integration test on a fresh /tmp/ data-repo with no .sops.yaml.
if [[ ! -f "$DATA_REPO/.sops.yaml" ]]; then
    rm -f "$SECRETS_FILE"  # Wipe the plaintext placeholders we just wrote.
    error "$DATA_REPO/.sops.yaml not found — sops cannot encrypt without recipient config"
    error "Plaintext placeholder file wiped (no real secrets, but cleaned up anyway)"
    error ""
    error "Fix: ensure $DATA_REPO is the actual bubble-vps-data repo (with .sops.yaml)"
    error "OR initialize a fresh data repo with .sops.yaml first"
    exit 4
fi

# Run sops; capture exit code explicitly. set -e in compound statements with
# subshells doesn't always propagate the way you'd expect (verified in this
# very debug session 2026-05-09 — set -euo pipefail script with `( cd && sops )`
# silently swallowed sops's non-zero exit).
SOPS_EXIT=0
( cd "$DATA_REPO" && sops --encrypt --input-type dotenv --output-type dotenv --in-place \
    "tenants/$TENANT_NAME/secrets.sops.env" ) || SOPS_EXIT=$?

if [[ "$SOPS_EXIT" -ne 0 ]]; then
    rm -f "$SECRETS_FILE"  # Wipe plaintext on encryption failure.
    error "sops --encrypt failed (exit $SOPS_EXIT). Plaintext placeholder file wiped."
    error "Common causes: .sops.yaml has no creation_rule matching tenants/<name>/secrets.sops.env,"
    error "  OR the operator master age key file is missing,"
    error "  OR sops binary is not installed (brew install sops)"
    exit 5
fi

# Defense in depth: verify the file is actually encrypted before declaring success.
# An encrypted SOPS dotenv file always contains 'ENC[' markers in the values.
if ! grep -q 'ENC\[' "$SECRETS_FILE"; then
    error "secrets.sops.env exists but appears NOT encrypted (no ENC[ markers found)"
    error "Wiping the file as a safety measure."
    rm -f "$SECRETS_FILE"
    exit 6
fi

info "encrypted secrets.sops.env (recipient: operator master key)"

# ─── 6. Friendly success summary ───────────────────────────────────────────
# 6. Bubble-ops-loop integration note
info "bubble-ops-loop framework will be auto-installed during deploy"
info "Notion: ${NOTION_API_KEY:+configured}${NOTION_API_KEY:-optional (not set)}"

heading "Done — scaffolding complete"

cat <<EOF

Tenant ${BOLD}$TENANT_NAME${NC} scaffolded at:
    $TENANT_DIR

Files created:
    tenant.yaml
    secrets.sops.env       (encrypted, placeholder values)
    README.md
    persona/$PERSONA_NAME/CLAUDE.md
    persona/$PERSONA_NAME/workspace/CLAUDE.md

${BOLD}Next steps:${NC}
  1. Edit ${YELLOW}$TENANT_DIR/tenant.yaml${NC} — replace ALL ${YELLOW}PLACEHOLDER_*${NC} values
     (host.ip, contact.primary_email, contact.primary_telegram_user_id, etc.)

  2. Paste real secret values via:
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=TELEGRAM_BOT_TOKEN
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=CLAUDE_CODE_OAUTH_TOKEN
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=TAILSCALE_AUTHKEY
       scripts/operator-set-secret.sh --tenant=$TENANT_NAME --key=PHONEHOME_TOKEN
     (Generate PHONEHOME_TOKEN with: openssl rand -hex 32)

  3. Fill in ${YELLOW}persona/$PERSONA_NAME/CLAUDE.md${NC} with the agent identity & mandate.

  4. Provision the Hetzner box (Step 7b's pyinfra task — fills in host.ip etc.)

  5. Deploy:
       cd $PLATFORM_ROOT
       ./deploy.sh --tenant=$TENANT_NAME

${DIM}The plaintext secrets.sops.env never existed beyond a brief moment on disk.
Only the encrypted form (with placeholder values) remains.${NC}

EOF
