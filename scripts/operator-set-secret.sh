#!/usr/bin/env bash
# Set or update a single secret in a SOPS-encrypted .env file.
#
# User-friendly: opens a native GUI password prompt (no typing in terminal,
# no scrollback echo). Safe for non-technical operators / clients.
#
# Usage (tenant mode — Hetzner / bubble-vps):
#   ./scripts/operator-set-secret.sh --tenant=<name> --key=<KEY> [--label="Friendly label"]
#
# Usage (project mode — any Mac-local workspace with .sops.yaml + secrets.sops.env):
#   ./scripts/operator-set-secret.sh --project=<path> --key=<KEY> [--label="Friendly label"]
#
# Examples:
#   ./scripts/operator-set-secret.sh --tenant=bubble-internal --key=TELEGRAM_BOT_TOKEN \
#       --label="Paste the Telegram bot token from @BotFather"
#
#   ./scripts/operator-set-secret.sh --tenant=acme-corp --key=OPENROUTER_API_KEY
#
#   ./scripts/operator-set-secret.sh --project=~/claude-workspaces/maya --key=TELEGRAM_BOT_TOKEN
#
# Usage (vps-dept mode — a SOPS file that lives ONLY on the VPS, root-owned):
#   ./scripts/operator-set-secret.sh --vps-dept=<slug> --key=<KEY> [--vps-host=<ssh>] [--label="..."]
#   Prompts locally (masked), pipes the value over SSH to the canonical
#   `morty-sops-add-key` tool which edits /etc/bubble/secrets-<slug>.sops.env
#   on the box (decrypt→set→re-encrypt→backup→audit-log in tmpfs). --vps-host
#   defaults to `hetzner`.
#
#   Example: set the long-lived OAuth token for Maya's dept agent:
#       ./scripts/operator-set-secret.sh --vps-dept=maya --key=CLAUDE_CODE_OAUTH_TOKEN \
#           --label="Paste the claude setup-token (sk-ant-oat01-...)"
#
# REMOTE PROMPT (pop the password dialog on a different Mac over Tailscale/SSH):
#   ./scripts/operator-set-secret.sh --project=<path> --key=<KEY> \
#       --remote-prompt=<ssh-host> [--label="..."]
#
#   Example (prompt opens on {{OPERATOR_2}}'s Mac, SOPS encrypt happens locally on {{OPERATOR}}-Mac):
#     ./scripts/operator-set-secret.sh \
#         --project=~/claude-workspaces/maya \
#         --key=GOOGLE_API_KEY \
#         --remote-prompt=macbook-air-2 \
#         --label="{{OPERATOR_2}} — paste the Google API key"
#
#   Security: secret travels remote-keyboard → remote osascript stdout → SSH (encrypted)
#             → local script stdin → SOPS encrypt. Never touches disk in plaintext on
#             either Mac. Tailscale's WireGuard provides E2E network encryption on top.
#
#   Setup needed (one-time): SSH access from this Mac to the remote host. Either:
#     - Tailscale SSH (`tailscale set --ssh` on the remote — but NOT supported on
#       sandboxed macOS GUI Tailscale builds — see Tailscale docs)
#     - Standard SSH: enable Remote Login on remote Mac (System Settings > General
#       > Sharing > Remote Login), then `ssh-copy-id <host>` from this Mac
#
# Requirements:
#   - macOS (uses osascript) OR Linux with `gum` installed
#   - sops installed
#   - SOPS_AGE_KEY_FILE set (defaults to ~/.config/sops/age/keys.txt)
#   - Tenant mode: tenant must already exist with a valid secrets.sops.env (created by Phase B)
#   - Project mode: workspace must contain .sops.yaml + secrets.sops.env at root
#   - --remote-prompt mode: SSH access to the remote host (passwordless preferred)
#
# Boilerplate part of bubble-vps-platform — works for any tenant or local project.

set -euo pipefail

# Secret-leak hardening (2026-05-29 Eliot audit): keep the brief plaintext
# window during decrypt/edit/re-encrypt from ever being world-readable.
umask 077

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
info()  { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}!${NC} %s\n" "$*"; }
error() { printf "${RED}✗${NC} %s\n" "$*" >&2; }

# ─── Argument parsing ──────────────────────────────────────────────────────
TENANT=""
PROJECT=""
KEY=""
LABEL=""
REMOTE_PROMPT=""
VPS_DEPT=""
VPS_HOST="hetzner"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tenant=*)         TENANT="${1#--tenant=}"; shift ;;
        --project=*)        PROJECT="${1#--project=}"; shift ;;
        --key=*)            KEY="${1#--key=}"; shift ;;
        --label=*)          LABEL="${1#--label=}"; shift ;;
        --remote-prompt=*)  REMOTE_PROMPT="${1#--remote-prompt=}"; shift ;;
        # vps-dept mode: set a key in a per-dept SOPS file that lives ONLY on
        # the VPS (/etc/bubble/secrets-<slug>.sops.env). The prompt still runs
        # locally (masked); the value is piped over SSH to the canonical,
        # audited `morty-sops-add-key` tool on the box. No local SOPS file.
        --vps-dept=*)       VPS_DEPT="${1#--vps-dept=}"; shift ;;
        --vps-host=*)       VPS_HOST="${1#--vps-host=}"; shift ;;
        --help|-h)
            grep '^#' "$0" | head -60 | sed 's/^# //;s/^#//'
            exit 0
            ;;
        *) error "unknown arg: $1"; exit 2 ;;
    esac
done

# Mutual exclusion: exactly one of --tenant / --project / --vps-dept must be set
__MODE_COUNT=0
[[ -n "$TENANT" ]]   && __MODE_COUNT=$((__MODE_COUNT+1))
[[ -n "$PROJECT" ]]  && __MODE_COUNT=$((__MODE_COUNT+1))
[[ -n "$VPS_DEPT" ]] && __MODE_COUNT=$((__MODE_COUNT+1))
if [[ "$__MODE_COUNT" -gt 1 ]]; then
    error "--tenant / --project / --vps-dept are mutually exclusive — specify exactly one"
    exit 2
fi
if [[ "$__MODE_COUNT" -eq 0 ]]; then
    error "specify one of --tenant=<name>, --project=<path>, or --vps-dept=<slug>"
    exit 2
fi

[[ -z "$KEY" ]] && { error "missing --key=<KEY>"; exit 2; }

# Validate KEY format (must be UPPER_SNAKE_CASE per SPEC-001)
if ! [[ "$KEY" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
    error "key '$KEY' must be UPPER_SNAKE_CASE (e.g. TELEGRAM_BOT_TOKEN)"
    exit 2
fi

[[ -z "$LABEL" ]] && LABEL="Paste the value for $KEY"

# ─── Paths ─────────────────────────────────────────────────────────────────
# vps-dept mode resolves no LOCAL file (the SOPS file + age key live on the
# box). Skip local path/age resolution; set a display name for the prompt.
if [[ -n "$VPS_DEPT" ]]; then
    SECRETS_FILE=""
    DISPLAY_NAME="vps-dept $VPS_DEPT (/etc/bubble/secrets-${VPS_DEPT}.sops.env on $VPS_HOST)"
elif [[ -n "$PROJECT" ]]; then
    # Project mode — any Mac-local workspace with .sops.yaml + secrets.sops.env
    # Expand tilde manually since `cd` doesn't expand it in all contexts
    PROJECT="${PROJECT/#\~/$HOME}"
    PROJECT_DIR=$(cd "$PROJECT" 2>/dev/null && pwd) || { error "project directory not found: $PROJECT"; exit 1; }
    SECRETS_FILE="$PROJECT_DIR/secrets.sops.env"
    SOPS_CWD="$PROJECT_DIR"
    DISPLAY_NAME="project $(basename "$PROJECT_DIR")"
else
    # Tenant mode — original behavior (unchanged)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PLATFORM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    DATA_REPO="${BUBBLE_DATA_REPO:-$(cd "$PLATFORM_ROOT/../bubble-vps-data" && pwd)}"
    TENANT_DIR="$DATA_REPO/tenants/$TENANT"
    SECRETS_FILE="$TENANT_DIR/secrets.sops.env"
    SOPS_CWD="$DATA_REPO"
    DISPLAY_NAME="tenant $TENANT"
fi

# Local file + age-key checks apply only to tenant/project modes (vps-dept
# does its SOPS work remotely on the box).
if [[ -z "$VPS_DEPT" ]]; then
    [[ -d "$(dirname "$SECRETS_FILE")" ]] || { error "directory not found: $(dirname "$SECRETS_FILE")"; exit 1; }
    [[ -f "$SECRETS_FILE" ]] || { error "secrets file not found: $SECRETS_FILE (initialize it first)"; exit 1; }

    export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"
    [[ -f "$SOPS_AGE_KEY_FILE" ]] || { error "age key file not found: $SOPS_AGE_KEY_FILE"; exit 1; }
fi

if ! command -v sops >/dev/null 2>&1; then
    error "sops not installed. Run: brew install sops"
    exit 1
fi

# ─── Get the secret value via a friendly prompt ────────────────────────────
get_secret_macos() {
    # AppleScript — native macOS prompt with hidden answer (••••••)
    local prompt_text="$1"
    local title="Bubble — set secret for $DISPLAY_NAME"
    osascript <<EOF 2>/dev/null
        try
            set result to text returned of (display dialog "$prompt_text" ¬
                with title "$title" ¬
                default answer "" ¬
                with hidden answer ¬
                buttons {"Cancel", "Save"} ¬
                default button "Save")
            return result
        on error
            return "USER_CANCELLED"
        end try
EOF
}

get_secret_gum() {
    # Linux/cross-platform — charmbracelet gum
    local prompt_text="$1"
    gum input --password --prompt "$prompt_text " --placeholder "(value will not echo)"
}

get_secret_fallback() {
    # POSIX fallback — read -s (no echo to terminal)
    local prompt_text="$1"
    printf "${BOLD}%s${NC}\n" "$prompt_text"
    printf "${DIM}(your input will not be visible — this is normal)${NC}\n"
    printf "> "
    IFS= read -rs value
    printf "\n"
    echo "$value"
}

get_secret_remote_ssh() {
    # Pop a GUI dialog on a remote Mac over SSH (Tailscale or LAN).
    # The remote runs osascript; its stdout (the entered value) streams back
    # over SSH (encrypted). No plaintext file ever touches the remote disk.
    local prompt_text="$1"
    local remote_host="$2"
    local title="Bubble — set secret for $DISPLAY_NAME"

    # macOS-version note: on modern macOS (Sequoia/Tahoe+, kernel 25.x),
    # `launchctl asuser` from an SSH session fails with "Could not switch to
    # audit session 0x...: Operation not permitted". The good news: when SSH
    # user IS the console user, plain osascript inherits the GUI session
    # automatically and renders dialogs on screen. So we skip launchctl asuser.
    #
    # We DO validate SSH user == console user; if they differ, the dialog
    # would silently fail (no error, just empty return). Fail fast instead.

    local stderr_file
    stderr_file=$(mktemp -t remote-prompt-stderr-XXXXXX)

    local value
    value=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$remote_host" \
        "TITLE=$(printf '%q' "$title") PROMPT=$(printf '%q' "$prompt_text") bash -s" \
        <<'REMOTE_EOF' 2>"$stderr_file"
# Running on the remote Mac.
CONSOLE_USER=$(stat -f %Su /dev/console)
if [[ -z "$CONSOLE_USER" ]]; then
    echo "REMOTE_ERROR: no console user logged in on remote — GUI dialog cannot render" >&2
    exit 1
fi

if [[ "$(whoami)" != "$CONSOLE_USER" ]]; then
    echo "REMOTE_ERROR: SSH user '$(whoami)' is not the console user '$CONSOLE_USER' — dialog will not render" >&2
    exit 1
fi

# Run osascript directly. When SSH user == console user, the Aqua session
# is inherited automatically on modern macOS.
# 2>/dev/null on osascript to suppress "execution error" noise from Cancel.
result=$(/usr/bin/osascript <<OSA 2>/dev/null
    try
        set result to text returned of (display dialog "$PROMPT" ¬
            with title "$TITLE" ¬
            default answer "" ¬
            with hidden answer ¬
            buttons {"Cancel", "Save"} ¬
            default button "Save")
        return result
    on error
        return "USER_CANCELLED"
    end try
OSA
)

printf '%s' "$result"
REMOTE_EOF
    )
    local ssh_code=$?

    if [[ $ssh_code -ne 0 ]]; then
        error "remote prompt failed (exit $ssh_code):"
        cat "$stderr_file" >&2
        rm -f "$stderr_file"
        echo "USER_CANCELLED"
        return 1
    fi
    rm -f "$stderr_file"
    echo "$value"
}

VALUE=""
if [[ -n "$REMOTE_PROMPT" ]]; then
    info "popping prompt on remote Mac: $REMOTE_PROMPT (via SSH)…"
    VALUE=$(get_secret_remote_ssh "$LABEL" "$REMOTE_PROMPT")
elif [[ "$(uname -s)" == "Darwin" ]]; then
    VALUE=$(get_secret_macos "$LABEL")
elif command -v gum >/dev/null 2>&1; then
    VALUE=$(get_secret_gum "$LABEL")
else
    VALUE=$(get_secret_fallback "$LABEL")
fi

if [[ "$VALUE" == "USER_CANCELLED" || -z "$VALUE" ]]; then
    error "cancelled or empty value — nothing changed"
    exit 1
fi

# Strip leading/trailing whitespace AND embedded newlines/CR — paste artifacts
# are common (osascript dialogs, terminal copy with trailing \n, Windows CR).
# A dotenv value with an embedded newline breaks sops parser AND leaks fragments
# into stderr ("invalid dotenv input line: <fragment>"). Sanitize defensively.
# Use python for unambiguous behavior across tools/locales.
VALUE=$(__VAL__="$VALUE" python3 -c "
import os, sys
v = os.environ['__VAL__']
# Strip ALL whitespace at edges, normalize embedded newlines/CRs to nothing
# (a token should not contain whitespace; if it does, the user pasted wrong)
v = v.strip()
v = v.replace('\n', '').replace('\r', '')
sys.stdout.write(v)
")

if [[ -z "$VALUE" ]]; then
    error "value was whitespace-only after sanitization — nothing changed"
    exit 1
fi

# Sanity-check — credential tokens should be non-trivial. Reject obvious junk.
if [[ ${#VALUE} -lt 8 ]]; then
    error "value is suspiciously short (${#VALUE} chars) — refusing to write. Re-run if this was intentional after raising minimum length."
    exit 1
fi

# ─── vps-dept mode: write to the per-dept SOPS file ON the VPS ──────────────
# The per-dept SOPS files (/etc/bubble/secrets-<slug>.sops.env) exist only on
# the box and are root-owned. We don't touch them locally — we pipe the value
# over SSH to the canonical, audited `morty-sops-add-key` tool, which does the
# decrypt → set → re-encrypt → backup → audit-log in /run tmpfs as root.
# Plaintext path: local prompt → ssh stdin (encrypted) → tmpfs on box. Never
# on disk plaintext, never in argv, never in this transcript.
if [[ -n "$VPS_DEPT" ]]; then
    if ! [[ "$VPS_DEPT" =~ ^[a-z][a-z0-9-]*$ ]]; then
        error "vps-dept '$VPS_DEPT' must be lowercase kebab (e.g. maya)"; exit 2
    fi
    REMOTE_SOPS="/etc/bubble/secrets-${VPS_DEPT}.sops.env"
    info "Setting $KEY in $REMOTE_SOPS on $VPS_HOST (via morty-sops-add-key) …"
    if printf '%s' "$VALUE" | ssh "$VPS_HOST" \
        "sudo -n /usr/local/bin/morty-sops-add-key '$REMOTE_SOPS' '$KEY'"; then
        info "$KEY set in $REMOTE_SOPS on $VPS_HOST."
        info "Restart the dept to pick it up: ssh $VPS_HOST 'sudo systemctl restart ops-loop-${VPS_DEPT}'"
        exit 0
    else
        error "remote morty-sops-add-key failed for $VPS_DEPT (see SSH output above)."
        exit 1
    fi
fi

# ─── Write the new value into the SOPS file ────────────────────────────────
# `sops --set` does an in-place atomic update of a single key.
# Need to wrap value in JSON for sops --set: `["KEY"] "value"` for dotenv format.
# Simpler: decrypt → modify → re-encrypt, all in a tmpfs file we shred immediately.

info "Updating $KEY in $SECRETS_FILE …"

# IMPORTANT: sops uses .sops.yaml's `creation_rules.path_regex` to decide WHICH
# recipients to encrypt with. The regex is matched against the FILE PATH at
# encryption time. So we must write the updated plaintext directly to the
# target path (NOT a /tmp file), then `sops --encrypt --in-place` it.
#
# ALSO IMPORTANT: sops walks UP from the current working directory (NOT from
# the file path) to find .sops.yaml. So we must cd into SOPS_CWD before any
# sops invocation (tenant mode: DATA_REPO; project mode: PROJECT_DIR).
cd "$SOPS_CWD"

# Backup the encrypted file in case anything fails.
BACKUP=$(mktemp -t sops-backup-XXXXXX)
cp "$SECRETS_FILE" "$BACKUP"

# sops stderr can contain VALUE FRAGMENTS on parse errors (e.g. "invalid dotenv
# input line: <fragment-of-value>"). To prevent transcript leaks if the script
# fails on encrypt, we capture sops stderr to a tmp file and surface ONLY a
# generic message + the file path on error. The full sops error is available
# for debugging by reading the tmp file MANUALLY (operator opens it locally
# and shred-deletes it afterwards if it contains sensitive fragments).
SOPS_STDERR=$(mktemp -t sops-stderr-XXXXXX)

# Cleanup on ANY abnormal exit (Ctrl-C, crash, error): shred the plaintext
# intermediate if orphaned, and restore the encrypted backup so we never leave
# $SECRETS_FILE in plaintext (root cause of the 2026-05-29 world-readable leak).
_oss_cleanup() {
  rc=$?
  [ -e "${SECRETS_FILE}.plaintext" ] && { shred -u "${SECRETS_FILE}.plaintext" 2>/dev/null || rm -f "${SECRETS_FILE}.plaintext"; }
  if [ "$rc" -ne 0 ] && [ -n "${BACKUP:-}" ] && [ -e "$BACKUP" ]; then
    echo "ERROR (rc=$rc) - restoring encrypted backup. sops stderr at: ${SOPS_STDERR:-n/a} (review locally then shred)." >&2
    mv "$BACKUP" "$SECRETS_FILE"
  fi
}
trap _oss_cleanup EXIT INT TERM ERR

# Decrypt to the same path (overwrites the encrypted file with plaintext briefly).
sops --decrypt "$SECRETS_FILE" > "${SECRETS_FILE}.plaintext" 2>"$SOPS_STDERR"
chmod 600 "${SECRETS_FILE}.plaintext"
mv "${SECRETS_FILE}.plaintext" "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

# Update or insert the line — use python for safe escaping (avoids sed
# delimiter issues if the value contains slashes, ampersands, etc.).
# IMPORTANT: env-var-prefix form (VAR=val cmd) must come BEFORE the executable;
# bash treats it as positional args otherwise.
if grep -q "^${KEY}=" "$SECRETS_FILE"; then
    __KEY__="$KEY" __SECRET_VALUE__="$VALUE" __PATH__="$SECRETS_FILE" python3 -c "
import os
key = os.environ['__KEY__']
value = os.environ['__SECRET_VALUE__']
path = os.environ['__PATH__']
with open(path) as f:
    lines = f.readlines()
out = []
for line in lines:
    if line.startswith(key + '='):
        out.append(f'{key}={value}\n')
    else:
        out.append(line)
with open(path, 'w') as f:
    f.writelines(out)
"
else
    printf "%s=%s\n" "$KEY" "$VALUE" >> "$SECRETS_FILE"
fi

# Re-encrypt in place (path-regex matches because we're writing to the real path).
# Capture stderr to the same tmp file (overwrite — decrypt-stderr already used).
sops --encrypt --input-type dotenv --output-type dotenv --in-place "$SECRETS_FILE" 2>"$SOPS_STDERR"

# Backup served its purpose; remove
rm -f "$BACKUP"
# Wipe sops stderr now that the operation succeeded (no leaks to leave behind).
rm -f "$SOPS_STDERR"
# Success — disarm the cleanup trap (plaintext already mv'd away; backup removed).
trap - EXIT INT TERM ERR

# Sanity check: decrypt and verify the key landed.
# stderr captured to a fresh tmp file (NOT printed) — same defense as above.
SOPS_STDERR_VERIFY=$(mktemp -t sops-verify-stderr-XXXXXX)
if sops --decrypt "$SECRETS_FILE" 2>"$SOPS_STDERR_VERIFY" | grep -q "^${KEY}="; then
    info "${KEY} updated successfully in ${SECRETS_FILE}"
    rm -f "$SOPS_STDERR_VERIFY"
else
    error "post-update verification failed — key not found in re-decrypted file. sops stderr at: $SOPS_STDERR_VERIFY (review locally then delete)"
    exit 1
fi

# ─── Next steps ────────────────────────────────────────────────────────────
if [[ -n "$TENANT" ]]; then
    cat <<EOF

${BOLD}Next steps:${NC}
  1. Review the encrypted diff (it'll be opaque, that's expected):
       cd $DATA_REPO
       git diff tenants/$TENANT/secrets.sops.env
  2. Commit:
       git commit -am "Set $KEY for $TENANT"

${DIM}The plaintext value is gone — only the encrypted form remains on disk.${NC}

EOF
else
    cat <<EOF

${BOLD}Next steps:${NC}
  1. Review the encrypted diff (it'll be opaque, that's expected):
       cd $SOPS_CWD
       git diff secrets.sops.env
  2. Commit when ready (the encrypted file is safe to version):
       git add secrets.sops.env
       git commit -m "Set $KEY for $(basename "$SOPS_CWD")"

${DIM}The plaintext value is gone — only the encrypted form remains on disk.${NC}

EOF
fi
