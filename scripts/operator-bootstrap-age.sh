#!/usr/bin/env bash
# Phase A of Step 3 — operator-Mac bootstrap for the SOPS+age secrets layer.
#
# Idempotent: safe to re-run. Will not regenerate an existing key.
#
# Usage:
#   ./scripts/operator-bootstrap-age.sh
#
# Output:
#   - Installs sops + age via brew (if missing)
#   - Generates ~/.config/sops/age/keys.txt (master keypair) — ONLY if missing
#   - Prints the PUBLIC key (safe to share with Lab via Telegram)
#   - The PRIVATE key (in keys.txt) NEVER leaves this Mac

set -euo pipefail

# ─── Colors for human-readable output ──────────────────────────────────────
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

# ─── Step 1: brew install sops + age ───────────────────────────────────────
heading "Step 1: install sops + age"

if ! command -v brew >/dev/null 2>&1; then
    error "Homebrew not found. Install it first: https://brew.sh"
    exit 1
fi

if command -v sops >/dev/null 2>&1; then
    info "sops already installed: $(sops --version | head -1)"
else
    info "Installing sops…"
    brew install sops
fi

if command -v age >/dev/null 2>&1; then
    info "age already installed: $(age --version 2>&1 | head -1)"
else
    info "Installing age…"
    brew install age
fi

# ─── Step 2: generate master age keypair (idempotent) ──────────────────────
heading "Step 2: master age keypair"

KEY_DIR="$HOME/.config/sops/age"
KEY_FILE="$KEY_DIR/keys.txt"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [[ -f "$KEY_FILE" ]]; then
    warn "Master keypair already exists at $KEY_FILE — leaving it alone"
else
    age-keygen -o "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    info "Generated new master keypair → $KEY_FILE (mode 0600)"
fi

# ─── Step 3: extract + print the public key ────────────────────────────────
heading "Step 3: your public key"

PUBKEY=$(age-keygen -y "$KEY_FILE")

cat <<EOF

${BOLD}This is your master public key:${NC}

    ${GREEN}${PUBKEY}${NC}

${DIM}It's safe to share. Paste this single line back to Lab via Telegram.${NC}

${BOLD}This is your master PRIVATE key file:${NC}

    ${YELLOW}${KEY_FILE}${NC}  (mode 0600, owner you only)

${RED}${BOLD}NEVER${NC} paste the contents of ${YELLOW}${KEY_FILE}${NC} anywhere.
${RED}${BOLD}NEVER${NC} commit it to git.
${RED}${BOLD}NEVER${NC} share it with anyone, including Lab.

Without this private key, NO encrypted secret can be decrypted on this Mac.
With it, you (and only you) can decrypt every tenant's secrets.

EOF

# ─── Step 4: convenience reminder ──────────────────────────────────────────
heading "Next step (Phase B / C of the runbook)"

cat <<EOF

   1. Paste the public key above into Telegram → Lab
   2. Wait for Lab to confirm Phase B is done
   3. Lab will tell you when to run \`sops <file>\` to paste in your rotated keys

Full runbook: \`specs/SPEC-006-secrets-runbook.md\`

EOF
