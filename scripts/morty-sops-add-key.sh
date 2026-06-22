#!/bin/bash
# morty-sops-add-key.sh — runs on Morty, called via SSH from a controller machine.
# Adds a KEY=VALUE pair to a SOPS-encrypted .env file, reading VALUE from stdin.
#
# DEPLOYED LOCATION: /usr/local/bin/morty-sops-add-key (root:root 0750)
# This file in the repo is the canonical source — git-tracked for audit/review.
#
# Security guarantees:
#   1. set -eu, set +x (no shell tracing)
#   2. Plaintext only ever exists in: stdin pipe + /run/lock tmpfs file
#      (root-only, 0600) + sops process RAM during encrypt
#   3. shred -u on plaintext tmp file after encrypt
#   4. Re-encrypts to EXISTING age recipients (no silent recipient addition)
#   5. Refuses overwrite if KEY already exists (rotation must use a separate path)
#   6. Atomic mv into place, backs up prior ciphertext to .bak-<unixtime>
#   7. Audit log append at /var/log/bubble-security/secrets-port-<date>.log
#   8. Respects the sops-guard wrapper: every decrypt uses --output FILE
#      (sops-guard blocks decrypt-to-stdout outside trusted systemd services)
#   9. SOPS_AGE_KEY_FILE=/etc/age/key.txt exported (Morty canonical age key path)
#
# Usage (Mac controller → Morty):
#   cat ~/.config/<svc>/api_key | ssh {{VPS_HOST}} \
#       'sudo -n /usr/local/bin/morty-sops-add-key /etc/bubble/secrets-<dept>.sops.env <KEY_NAME>'
#
# Verify success (no value displayed):
#   ssh {{VPS_HOST}} "sudo -n bash -c 'SOPS_AGE_KEY_FILE=/etc/age/key.txt \
#     sops --decrypt --output /run/lock/v.\$\$ /etc/bubble/secrets-<dept>.sops.env && \
#     grep -c \"^<KEY_NAME>=\" /run/lock/v.\$\$ && shred -u /run/lock/v.\$\$'"
#
# Author: Rick (R&D), 2026-05-25, per {{OPERATOR}} msg 3231 ("be most careful about
# security of env secrets on VPS and add all necessary protections").
set -eu
set +x
export SOPS_AGE_KEY_FILE=/etc/age/key.txt

SOPS_FILE="${1:?usage: morty-sops-add-key <sops-file> <KEY>}"
KEY="${2:?usage: morty-sops-add-key <sops-file> <KEY>}"

[[ -f "$SOPS_FILE" ]] || { echo "ERR: $SOPS_FILE missing" >&2; exit 2; }
[[ -r "$SOPS_FILE" ]] || { echo "ERR: $SOPS_FILE not readable" >&2; exit 2; }
grep -q '^sops_age' "$SOPS_FILE" || { echo "ERR: $SOPS_FILE not SOPS-encrypted" >&2; exit 3; }

# Accept input with OR without trailing newline (sops --extract strips it)
IFS= read -r SECRET_VALUE || true
SECRET_VALUE="${SECRET_VALUE%$'\r'}"
SECRET_VALUE="${SECRET_VALUE%$'\n'}"
[[ -n "$SECRET_VALUE" ]] || { echo "ERR: empty value from stdin" >&2; exit 4; }

TMP_DIR=$(mktemp -d --tmpdir=/run/lock sops-port.XXXXXX 2>/dev/null) \
    || TMP_DIR=$(mktemp -d --tmpdir=/dev/shm sops-port.XXXXXX)
chmod 0700 "$TMP_DIR"
TMP_PLAIN="$TMP_DIR/plain.env"
TMP_ENC="$TMP_DIR/enc.sops.env"

cleanup() {
    if [[ -f "$TMP_PLAIN" ]]; then shred -u "$TMP_PLAIN" 2>/dev/null || rm -f "$TMP_PLAIN"; fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

sops --decrypt --input-type dotenv --output-type dotenv --output "$TMP_PLAIN" "$SOPS_FILE"
chmod 0600 "$TMP_PLAIN"

if grep -q "^${KEY}=" "$TMP_PLAIN"; then
    echo "ERR: ${KEY} already exists in $SOPS_FILE — refusing overwrite" >&2
    exit 5
fi

printf '%s=%s\n' "$KEY" "$SECRET_VALUE" >> "$TMP_PLAIN"

AGE_RECIPIENTS=$(grep -A 100 '^sops_age' "$SOPS_FILE" \
                 | grep '^sops_age__list_[0-9]*__map_recipient=' \
                 | sed 's/^.*recipient=//' \
                 | sed 's/^"//; s/"$//' \
                 | paste -sd, -)

[[ -n "$AGE_RECIPIENTS" ]] || { echo "ERR: could not extract age recipients" >&2; exit 6; }
RECIPIENT_COUNT=$(echo "$AGE_RECIPIENTS" | tr ',' '\n' | wc -l)

sops --encrypt --input-type dotenv --output-type dotenv \
    --age "$AGE_RECIPIENTS" \
    --output "$TMP_ENC" \
    "$TMP_PLAIN"

[[ -s "$TMP_ENC" ]] || { echo "ERR: re-encrypt produced empty file" >&2; exit 7; }
grep -q '^sops_age' "$TMP_ENC" || { echo "ERR: re-encrypted file missing sops_age" >&2; exit 7; }

BACKUP="${SOPS_FILE}.bak-$(date +%s)"
cp "$SOPS_FILE" "$BACKUP"
chmod 0440 "$BACKUP"
chown root:root "$BACKUP"
mv "$TMP_ENC" "$SOPS_FILE"
chmod 0440 "$SOPS_FILE"
chown root:root "$SOPS_FILE"

VERIFY_PLAIN="$TMP_DIR/verify.env"
sops --decrypt --input-type dotenv --output-type dotenv --output "$VERIFY_PLAIN" "$SOPS_FILE"
chmod 0600 "$VERIFY_PLAIN"
NEW_COUNT=$(grep -c "^${KEY}=" "$VERIFY_PLAIN" || echo 0)
shred -u "$VERIFY_PLAIN" 2>/dev/null || rm -f "$VERIFY_PLAIN"

if [[ "$NEW_COUNT" -ne 1 ]]; then
    echo "ERR: post-port verify: ${KEY} count=$NEW_COUNT (expected 1). Restoring backup." >&2
    mv "$BACKUP" "$SOPS_FILE"
    exit 8
fi

AUDIT_LOG="/var/log/bubble-security/secrets-port-$(date -u +%Y-%m-%d).log"
mkdir -p "$(dirname "$AUDIT_LOG")"
printf '%s PORT_OK file=%s key=%s recipients=%d backup=%s session=rick-tier1\n' \
    "$(date -u +%FT%TZ)" "$SOPS_FILE" "$KEY" "$RECIPIENT_COUNT" "$BACKUP" \
    >> "$AUDIT_LOG"
chmod 0640 "$AUDIT_LOG"

echo "OK key=${KEY} file=${SOPS_FILE} recipients=${RECIPIENT_COUNT} backup=${BACKUP}"
