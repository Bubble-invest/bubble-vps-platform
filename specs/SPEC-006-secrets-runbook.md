# SPEC-006 — Step 3 EXECUTION RUNBOOK (companion to SPEC-006)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08

This is the operational runbook for executing Step 3 (secrets layer). SPEC-006 (sibling doc) describes the architecture; this doc describes WHO does WHAT, in WHAT order, to bring it up safely without leaking secrets through the chat transcript.

---

## Three actors

- **{{OPERATOR}}** (operator, holds the master age key + the rotated secret values)
- **Lab parent agent** (orchestrates, writes platform code, runs validation)
- **Subagent** (does the mechanical box-side install)

The rotated secrets MUST never appear in any agent's transcript. They go directly from {{OPERATOR}}'s keyboard into the SOPS-encrypted file.

---

## Phase A — Operator-Mac bootstrap ({{OPERATOR}}'s hands, ~5 min)

These are one-time setup steps {{OPERATOR}} does on his Mac.

### A.1. Install tooling
```bash
brew install sops age
sops --version  # expect 3.x
age --version
```

### A.2. Generate the master age keypair
```bash
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt
age-keygen -y ~/.config/sops/age/keys.txt
# Prints the public key — looks like: age1abc...xyz
```

The PUBLIC key (output of the last command) is what Lab will use in `.sops.yaml`. The PRIVATE key in `~/.config/sops/age/keys.txt` NEVER leaves the Mac.

### A.3. Tell Lab the public key

{{OPERATOR}} pastes ONLY the public key (starts with `age1`) in Telegram. Public keys are safe to share — they encrypt, they don't decrypt.

→ Lab updates `.sops.yaml` with this recipient.

---

## Phase B — Lab prepares scaffolding (Lab parent agent, ~5 min)

No secrets touched. All operator-on-Mac.

### B.1. Create `.sops.yaml`
Single file in `bubble-vps-data/.sops.yaml` with the master pubkey as recipient for `tenants/bubble-internal/secrets.sops.env`.

### B.2. Create empty encrypted file
```bash
cd bubble-vps-data
SOPS_AGE_RECIPIENTS=age1abc...xyz sops --encrypt \
    --input-type dotenv --output-type dotenv \
    /dev/null > tenants/bubble-internal/secrets.sops.env
```

This produces an encrypted file containing zero key/value pairs but with valid SOPS metadata. {{OPERATOR}} will populate it next.

Or simpler (a shape we can iterate on):
```bash
cat > /tmp/initial-secrets.env <<'EOF'
# Placeholders — {{OPERATOR}} fills these in via `sops <file>` next.
OPENROUTER_API_KEY=PASTE_HERE
TELEGRAM_BOT_TOKEN=PASTE_HERE
TAILSCALE_AUTHKEY=PASTE_HERE_OR_LEAVE_AS_PLACEHOLDER_UNTIL_STEP_6
EOF

cd bubble-vps-data
sops --encrypt --input-type dotenv --output-type dotenv \
    /tmp/initial-secrets.env > tenants/bubble-internal/secrets.sops.env
shred -u /tmp/initial-secrets.env  # or: rm + verify shred unavailable on macOS, just rm + sleep
```

The encrypted file is committed to the data repo. The plaintext intermediate is wiped immediately.

### B.3. Update SPEC + tenant.yaml
Add the `secrets:` block to `tenants/bubble-internal/tenant.yaml` per SPEC-006 (with `required_keys: [OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN]` for now — we'll add TAILSCALE_AUTHKEY at Step 6).

---

## Phase C — {{OPERATOR}} paste-in ({{OPERATOR}}'s hands, ~2 min)

```bash
cd ~/claude-workspaces/rnd/projects/bubble-vps-data
sops tenants/bubble-internal/secrets.sops.env
# Opens in $EDITOR with placeholders visible
# {{OPERATOR}} replaces PASTE_HERE with the rotated values
# Saves — sops encrypts the file again automatically
git status
git diff --stat tenants/bubble-internal/secrets.sops.env  # encrypted diff is opaque, that's expected
git commit -am "Step 3: populate bubble-internal secrets"
```

{{OPERATOR}} confirms in Telegram: "secrets populated, ready for box-side."

NOT in transcript: the secret values themselves.

---

## Phase D — Subagent runs box-side install (Lab subagent, ~10 min)

The subagent does:

### D.1. Install sops + age on the box
- Download age binary (from GitHub release) to `/usr/local/bin/age`
- Download sops binary (from GitHub release) to `/usr/local/bin/sops`
- Verify both run as expected

### D.2. Generate per-tenant box age key
- `age-keygen -o /etc/age/key.txt && chmod 0400 /etc/age/key.txt && chown root:root`
- Read the public key, save as `/etc/age/key.pub`
- pyinfra `files.get` → copy box pubkey back to operator Mac at `bubble-vps-data/tenants/bubble-internal/box-pubkey.txt`

### D.3. STOP at the bootstrap gate
After D.2, the deploy explicitly halts with a clear message:
```
🔑 Box pubkey generated: age1xyz...
🔑 Saved to: bubble-vps-data/tenants/bubble-internal/box-pubkey.txt

⚠ MANUAL STEP REQUIRED before re-running deploy:

   cd ~/claude-workspaces/rnd/projects/bubble-vps-data
   # Add box-pubkey.txt content to .sops.yaml as a recipient for bubble-internal
   sops updatekeys tenants/bubble-internal/secrets.sops.env
   git commit -am "Step 3: add box pubkey to bubble-internal recipients"

After that, re-run: ./scripts/deploy.sh --tenant=bubble-internal
```

This is the ONE manual operator step in the bootstrap. Future deploys (e.g. when {{OPERATOR}} rotates a key) skip it.

### D.4. (After re-run, the gate is past)
- rsync `bubble-vps-data/tenants/bubble-internal/secrets.sops.env` → `/etc/bubble/secrets.sops.env`
- mode 0440, owner root:root
- Test-decrypt on the box: `sudo sops --decrypt /etc/bubble/secrets.sops.env > /tmp/test`
- Validate `required_keys` all present in /tmp/test
- `shred /tmp/test` (or rm + zero confidence — the key here is ephemeral)

### D.5. NO cleanup of legacy plaintext yet
Step 3's purpose is to install the encrypted layer. The legacy plaintext files (8 known leaks) are removed at **Step 4** AFTER the systemd service is wired and verified.

This means after Step 3 the box has BOTH the encrypted layer (idle, not yet consumed) AND the old plaintext files (consumed by the running agent). Both worlds coexist briefly. Step 4 flips the switch.

---

## Phase E — Lab validation (Lab parent agent, ~3 min)

Re-run dogfood + verify all D.4 checks pass. Run `pytest`. Post checkpoint to {{OPERATOR}}.

---

## Why this split is safe

- **Master age private key** — never leaves {{OPERATOR}}'s Mac. Not in any transcript.
- **Rotated OR key + bot token** — go from {{OPERATOR}}'s keyboard directly into the SOPS file via `sops` invocation in his terminal. Never in any transcript.
- **Box age private key** — generated on the box, never leaves the box (only its public key is shared).
- **Public keys** — safe to share, but only via Telegram messages from {{OPERATOR}} (control point) to Lab. Lab does not generate any keys.
- **Encrypted secrets file** — lives in git (data repo). Even if accidentally pushed public, it cannot be decrypted without one of the recipient private keys.

---

## What Step 3 deliberately does NOT do

- Does not start a systemd service (Step 4)
- Does not stop the running tmux agent (Step 4)
- Does not delete the plaintext leak files (Step 4)
- Does not configure Tailscale or phone-home (Step 6)

After Step 3, you have:
- ✅ A working encrypted-secrets pipeline, end-to-end, verified
- ⚠ The OLD agent still running with stale keys (broken since the rotation)
- ⚠ 8 plaintext files still on disk (will be deleted at Step 4)

This is the right place to checkpoint with {{OPERATOR}}. Step 4 is the cutover.
