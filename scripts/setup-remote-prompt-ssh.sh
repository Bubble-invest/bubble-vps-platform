#!/usr/bin/env bash
# Bootstrap SSH access from this Mac to a remote Mac on the Tailscale network,
# so `operator-set-secret.sh --remote-prompt=<host>` can pop password dialogs
# on the remote Mac (e.g. {{OPERATOR_2}}'s Mac while she's at a different physical Mac).
#
# Usage:
#   ./setup-remote-prompt-ssh.sh <remote-host>
#
#   Example:
#     ./setup-remote-prompt-ssh.sh macbook-air-2
#
# What this script does:
#   1. Checks the remote is reachable via Tailscale ping
#   2. Verifies SSH is enabled on the remote (probes port 22)
#   3. Copies this Mac's pubkey to the remote's authorized_keys (interactive — you'll be asked for the remote user's password ONCE)
#   4. Verifies passwordless SSH works after key copy
#   5. Confirms the remote can render osascript dialogs (console user check)
#
# Prerequisites on the REMOTE Mac (one-time setup by remote user):
#   - System Settings > General > Sharing > Remote Login: ON
#   - User logged in to the GUI (Aqua session — needed for osascript display dialog)

set -uo pipefail

REMOTE_HOST="${1:-}"
if [[ -z "$REMOTE_HOST" ]]; then
    echo "Usage: $0 <remote-host>"
    echo "Example: $0 {{OPERATOR_2_HOST}}"
    echo "         $0 jade@{{OPERATOR_2_HOST}}"
    exit 2
fi

# Split user@host if present — Tailscale ping wants just the host part
if [[ "$REMOTE_HOST" == *"@"* ]]; then
    REMOTE_USER="${REMOTE_HOST%@*}"
    REMOTE_HOSTNAME="${REMOTE_HOST#*@}"
else
    REMOTE_USER=""
    REMOTE_HOSTNAME="$REMOTE_HOST"
fi

# Colors
if [[ -t 1 ]]; then
    G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
else
    G="" Y="" R="" B="" D="" N=""
fi
ok()    { printf "${G}✓${N} %s\n" "$*"; }
warn()  { printf "${Y}!${N} %s\n" "$*"; }
err()   { printf "${R}✗${N} %s\n" "$*" >&2; }
step()  { printf "\n${B}▸ %s${N}\n" "$*"; }

# ─── Step 1: Tailscale reachability ────────────────────────────────────────
step "Step 1 — Tailscale reachability"
if ! command -v tailscale >/dev/null 2>&1; then
    err "tailscale CLI not found — install via Homebrew or Mac App Store"
    exit 1
fi

if tailscale ping --c 1 "$REMOTE_HOSTNAME" 2>&1 | grep -q "pong from"; then
    ok "$REMOTE_HOSTNAME is reachable on Tailscale"
else
    err "$REMOTE_HOSTNAME is NOT reachable on Tailscale"
    echo "   Check: 1) Tailscale is running on both Macs, 2) hostname matches a node in 'tailscale status'"
    exit 1
fi

# ─── Step 2: SSH port probe ────────────────────────────────────────────────
step "Step 2 — SSH port 22 probe"
if nc -z -w 3 "$REMOTE_HOSTNAME" 22 2>/dev/null; then
    ok "Port 22 is open on $REMOTE_HOSTNAME"
else
    err "Port 22 is closed on $REMOTE_HOSTNAME"
    echo "   On the remote Mac: System Settings > General > Sharing > Remote Login: ON"
    echo "   (Or run: sudo systemsetup -setremotelogin on)"
    exit 1
fi

# ─── Step 3: pubkey discovery ──────────────────────────────────────────────
step "Step 3 — Local SSH pubkey"
PUBKEY=""
for k in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
    if [[ -f "$k" ]]; then
        PUBKEY="$k"
        ok "Using pubkey: $PUBKEY"
        break
    fi
done

if [[ -z "$PUBKEY" ]]; then
    err "No SSH key found at ~/.ssh/id_ed25519 or ~/.ssh/id_rsa"
    echo "   Generate one: ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519"
    exit 1
fi

# ─── Step 4: Check if passwordless SSH already works ───────────────────────
step "Step 4 — Check current SSH status"
if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$REMOTE_HOST" "echo ok" 2>/dev/null | grep -q "ok"; then
    ok "Passwordless SSH ALREADY works — skipping ssh-copy-id"
    NEED_COPY=0
else
    warn "Passwordless SSH does not yet work — running ssh-copy-id"
    NEED_COPY=1
fi

# ─── Step 5: ssh-copy-id (interactive — needs remote password ONCE) ────────
if [[ $NEED_COPY -eq 1 ]]; then
    step "Step 5 — Copy pubkey to remote (you will be asked for the REMOTE user's password ONCE)"
    echo ""
    echo "  ${D}If the remote user (on $REMOTE_HOST) differs from your local username,"
    echo "  abort and re-run as: $0 <user>@$REMOTE_HOST${N}"
    echo ""
    # IdentitiesOnly=yes prevents SSH from cycling through all keys in ~/.ssh and
    # hitting "Too many authentication failures" before the password prompt lands.
    # PreferredAuthentications=password forces SSH to fall back to password auth
    # (since we KNOW the key isn't installed yet — we're about to install it).
    if ssh-copy-id -i "$PUBKEY" \
        -o StrictHostKeyChecking=accept-new \
        -o IdentitiesOnly=yes \
        -o PreferredAuthentications=password \
        -o PubkeyAuthentication=no \
        "$REMOTE_HOST"; then
        ok "pubkey copied to $REMOTE_HOST"
    else
        err "ssh-copy-id failed — see above for the SSH error"
        exit 1
    fi

    # Verify the copy worked
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_HOST" "echo ok" 2>/dev/null | grep -q "ok"; then
        ok "Passwordless SSH now works ✓"
    else
        err "ssh-copy-id finished but passwordless SSH still doesn't work — investigate manually"
        exit 1
    fi
fi

# ─── Step 6: Check remote can render osascript dialogs ─────────────────────
step "Step 6 — Verify remote can render GUI dialogs (console user check)"
CONSOLE_USER=$(ssh -o BatchMode=yes "$REMOTE_HOST" "stat -f %Su /dev/console" 2>/dev/null)
SSH_USER=$(ssh -o BatchMode=yes "$REMOTE_HOST" "whoami" 2>/dev/null)

if [[ -z "$CONSOLE_USER" ]]; then
    err "could not determine console user on remote"
    exit 1
fi

if [[ "$CONSOLE_USER" == "$SSH_USER" ]]; then
    ok "Remote console user ($CONSOLE_USER) == SSH user — GUI dialogs will render correctly"
else
    warn "Remote console user ($CONSOLE_USER) != SSH user ($SSH_USER)"
    echo "   GUI dialogs may not render unless $SSH_USER can sudo to $CONSOLE_USER without password"
    echo "   Recommend: log in to the remote Mac as $SSH_USER (the SSH user) in the GUI"
fi

# ─── Done ──────────────────────────────────────────────────────────────────
step "Done"
echo ""
echo "  You can now run:"
echo ""
echo "    ${B}operator-set-secret.sh --project=<path> --key=<KEY> --remote-prompt=$REMOTE_HOST${N}"
echo ""
echo "  The password dialog will pop up on $REMOTE_HOST (visible to its console user)."
echo ""
