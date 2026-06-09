# SPEC-006 — Secrets layer (SOPS + age + systemd) — Step 3

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending {{OPERATOR}} approval_
**Depends on:** SPEC-001 (tenant.yaml), SPEC-002 (inventory), SPEC-003 (host data exposure), SPEC-005 (hardening must be complete)
**Implements:** Step 3 of the Bubble VPS Platform build plan

---

## Purpose

Replace the current plaintext-keys-on-disk situation with the industry-standard 2026 pattern: **SOPS + age + systemd `EnvironmentFile`**. Encrypted secrets live in the data repo; plaintext only exists in RAM (`/run/<service>/env`, tmpfs) for the duration of the agent's lifetime, never on disk.

Validates against the multi-layer threat model from `three-architectures-reference.md` and `end-state-vision.md`.

---

## Threat model

We are protecting against:

| Adversary | Capability | Mitigation |
|---|---|---|
| Attacker reading the data repo (e.g. accidental public push) | Reads encrypted blob | Cannot decrypt without an age private key |
| Attacker on the box with shell as `claude` | Reads /run/<service>/env (root-owned, 0400) | Cannot read — wrong UID |
| Attacker with root on the box | Can read /run/<service>/env while service is running | Has full system control anyway; secrets are momentarily exposed but limited blast radius (key rotation triggers full re-encrypt) |
| Hetzner provider taking a snapshot | Reads disk image | tmpfs `/run/` is RAM-only; plaintext NEVER touches the disk |
| Operator-Mac compromise | Reads age private key on Mac | Mac is the root of trust; if it falls, ALL tenants are compromised. Mitigated by: macOS Keychain protection on the age key file, hardware-backed if FileVault enabled |
| Process inspection (`/proc/<pid>/environ`) | Anyone with same UID or root reads env vars | Same UID = the agent's own UID, fine. Root = catastrophic anyway |
| Transcript bleed (Claude reads .env into a JSONL) | Same risk we have on Mac side | Mitigated: agent never has direct read access to /run/<service>/env (systemd injects vars, agent inherits them; the file is root-only) |

The plaintext-on-disk attack surface is reduced to ZERO on the box. The only persistent secret material on the box is the age private key in `/etc/age/key.txt` (root:root 0400). Compromise of that key + filesystem read access = decrypt the data repo's secrets for that tenant.

---

## Architecture

```
┌──── Operator Mac ────────────────────────┐
│  Master age key:                          │
│     ~/.config/sops/age/keys.txt           │
│     (root of trust, NEVER copied)         │
│                                           │
│  bubble-vps-data/                         │
│  ├── .sops.yaml         ← per-recipient   │
│  │                        age public keys │
│  └── tenants/<name>/                      │
│      └── secrets.sops.env                 │
│         (encrypted, safe to commit)       │
└────┬──────────────────────────────────────┘
     │ pyinfra deploy
     ▼
┌──── Tenant box ──────────────────────────┐
│  Per-tenant age key:                      │
│     /etc/age/key.txt  (root:root 0400)    │
│                                           │
│  Encrypted file (deployed via pyinfra):   │
│     /etc/bubble/secrets.sops.env          │
│     (root:root 0440, safe-on-disk)        │
│                                           │
│  systemd service:                         │
│     ExecStartPre=/usr/local/bin/sops      │
│         --decrypt                          │
│         --output /run/claude-agent/env    │
│         /etc/bubble/secrets.sops.env      │
│     EnvironmentFile=/run/claude-agent/env │
│     ExecStart=/usr/bin/claude ...         │
│                                           │
│  /run/ is tmpfs (RAM-only, gone on reboot)│
└───────────────────────────────────────────┘
```

---

## Recipient model

A SOPS-encrypted file can decrypt if ANY of its recipients is in the local age keyring. We use a **two-recipient model** per tenant:

1. **The operator** ({{OPERATOR}}'s Mac) — has the master key. Can decrypt every tenant's secrets to edit them. Master key NEVER leaves the Mac.
2. **The tenant box itself** — has its own per-tenant age key generated during deploy. Can decrypt only its own secrets.

Add a third recipient when needed:

3. **Per-tenant break-glass key** (optional) — a printed/USB-stored age key the customer keeps for "rotate keys themselves if Bubble disappears" continuity. Rare, only for premium contracts.

`bubble-vps-data/.sops.yaml`:

```yaml
creation_rules:
  - path_regex: tenants/bubble-internal/secrets\.sops\.env$
    age: >-
      <operator-master-pubkey>,
      <bubble-internal-box-pubkey>

  - path_regex: tenants/[^/]+/secrets\.sops\.env$
    age: >-
      <operator-master-pubkey>,
      # box pubkey is appended by the new-tenant.sh script when the box is provisioned
```

This file lives in the data repo (committed). It's the only place SOPS recipient lists are managed.

---

## Per-tenant box keypair lifecycle

### Generation (during first deploy)

`tasks/secrets/age_setup.py` does, idempotently:

```bash
# On the box, as root:
test -f /etc/age/key.txt || (
    age-keygen -o /etc/age/key.txt
    chmod 0400 /etc/age/key.txt
    chown root:root /etc/age/key.txt
)

# Read the public key for reporting back:
age-keygen -y /etc/age/key.txt > /etc/age/key.pub
chmod 0444 /etc/age/key.pub
```

The pubkey is then SCP'd back to the operator's Mac (via pyinfra `files.get`) and printed in deploy output, prompting the operator to add it to `.sops.yaml`. This is a ONE-TIME bootstrap — once the pubkey is in `.sops.yaml` and the file is re-encrypted with both recipients, no further key shuffling is needed.

### First-deploy flow

```
1. Operator runs: ./scripts/deploy.sh --tenant=bubble-internal
2. pyinfra: hardening tasks run (Step 2 — already done)
3. pyinfra: tasks/secrets/age_setup.py runs
   - Generates /etc/age/key.txt if absent
   - Reports the box's pubkey (e.g. age1abc...xyz) to operator's terminal
4. Operator (manual ONCE):
   - Adds the pubkey to bubble-vps-data/.sops.yaml as a recipient for this tenant
   - Re-encrypts the secrets file: `sops updatekeys tenants/bubble-internal/secrets.sops.env`
   - Commits + pushes the data repo
5. Operator re-runs: ./scripts/deploy.sh --tenant=bubble-internal
6. pyinfra: tasks/secrets/sops_deploy.py
   - rsync's the encrypted file to /etc/bubble/secrets.sops.env
   - Test-decrypts it ON THE BOX as root (sanity check) — must succeed using the box's key
   - Sets up the systemd ExecStartPre
```

Steps 4 is the only manual step. Future deploys (after step 4 is done once) are fully automated.

### Rotation

When a secret value changes (e.g. OpenRouter key rotated):

```bash
# On operator Mac:
cd bubble-vps-data
sops tenants/<name>/secrets.sops.env
# Edit in $EDITOR — values appear plaintext, encrypted again on save
git commit -am "Rotate <secret>"
git push

# Then deploy:
./scripts/deploy.sh --tenant=<name>
```

pyinfra detects the file changed (hash diff), re-rsyncs, restarts the systemd unit (which decrypts with the box's age key into a new /run/<service>/env). Total downtime ~2-5 seconds.

For ALL tenants at once: `./scripts/deploy.sh --tenants=all` after editing one secret in each.

---

## File layout on the box

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/etc/age/key.txt` | root:root | 0400 | The box's age private key |
| `/etc/age/key.pub` | root:root | 0444 | The box's age public key (informational) |
| `/etc/bubble/secrets.sops.env` | root:root | 0440 | Encrypted secrets blob, deployed from data repo |
| `/run/claude-agent/env` | root:root | 0400 | Decrypted plaintext, tmpfs (RAM-only) |
| `/usr/local/bin/sops` | root:root | 0755 | The sops binary, installed by pyinfra |
| `/usr/local/bin/age` | root:root | 0755 | The age binary, installed by pyinfra |

`/run/` is **tmpfs by default on Linux** (mounted at boot, RAM-backed, contents gone on reboot/restart). Storing decrypted secrets here is the standard pattern; verified by SPEC-006 reference (DCHost guide).

---

## tenant.yaml additions

The hardening section already exists. Add a `secrets` section:

```yaml
secrets:
  enabled: true                                        # REQUIRED. Whether secrets layer is active.
  age_key_path: /etc/age/key.txt                       # OPTIONAL, default shown.
  encrypted_file_path: /etc/bubble/secrets.sops.env    # OPTIONAL, default shown.
  decrypted_runtime_path: /run/claude-agent/env        # OPTIONAL, default shown. Used by systemd unit.

  # Required entries that secrets.sops.env MUST contain:
  required_keys:
    - OPENROUTER_API_KEY
    - TELEGRAM_BOT_TOKEN
    - TAILSCALE_AUTHKEY
```

The `required_keys` list is checked at deploy time (sops decrypt + grep). If any are missing, deploy fails before installing the systemd unit.

---

## pyinfra task layout

```
bubble-vps-platform/pyinfra/tasks/secrets/
├── __init__.py
├── deploy.py                ← public entrypoint, called from deploy.py
├── _binaries.py             ← installs sops + age on the box
├── _age_setup.py            ← generates /etc/age/key.txt if missing
├── _sops_deploy.py          ← rsyncs encrypted file from data repo, validates decryption
└── _systemd_unit.py         ← (deferred to Step 4 — agent install) — adds ExecStartPre to systemd unit
```

For Step 3, we focus on _binaries, _age_setup, _sops_deploy. The systemd integration lands in Step 4 with the agent install.

---

## Pre-requisites for Step 3 to start

These must be true BEFORE we run Step 3:

1. ✅ Step 1 done (platform repo + inventory works)
2. ✅ Step 2 done (hardening idempotent, dogfood passes)
3. ⚠️ **{{OPERATOR}} has rotated the leaked OpenRouter key** (he confirmed this 2026-05-08 msg 1619)
4. ⚠️ **{{OPERATOR}} has rotated/regenerated the Telegram bot token** (in progress 2026-05-08 msg 1619)
5. ⚠️ **Operator master age key exists on the Mac** — if not, generate it: `age-keygen -o ~/.config/sops/age/keys.txt && chmod 600 ~/.config/sops/age/keys.txt && export SOPS_AGE_RECIPIENTS=$(age-keygen -y ~/.config/sops/age/keys.txt)`
6. ⚠️ **`sops` and `age` installed on operator's Mac**: `brew install sops age`

Items 3-6 are blocking; Lab will check before kicking off the Step 3 implementation.

---

## What Step 3 deploys (operationally)

1. Install `sops` (~6 MB) and `age` (~2 MB) on the box via apt or direct binary download
2. Generate `/etc/age/key.txt` on the box (idempotent — only if missing)
3. Read back the box's public key, print to operator
4. **Operator manual step:** add box pubkey to `.sops.yaml`, run `sops updatekeys`, commit data repo
5. (Re-deploy) Sync `secrets.sops.env` to `/etc/bubble/secrets.sops.env`
6. Test-decrypt on the box: `sudo sops --decrypt /etc/bubble/secrets.sops.env > /tmp/test-decrypt && grep -q OPENROUTER_API_KEY /tmp/test-decrypt && rm /tmp/test-decrypt`. If grep fails → required key missing, abort.
7. NOTE: at end of Step 3, the systemd unit is NOT yet wired up. The decrypted file is not actively used by anything. Step 4 wires the agent service to consume it.

---

## Test plan for Step 3

### Unit tests (offline)

- `test_sops_yaml_recipients_valid()` — load `.sops.yaml`, assert each tenant entry has at least 2 recipients (operator + box) for non-bootstrap state, OR exactly 1 (operator only) for pre-bootstrap state. Allow either.
- `test_required_keys_validation()` — given a fake decrypted env file, assert validator detects missing required keys
- `test_secrets_path_resolution()` — given a tenant.yaml, the inventory builds the right encrypted_file path

### Integration test (the real validation)

`tests/integration/test_secrets_dogfood.sh`:

```bash
# Pre-conditions on operator Mac:
#  - sops + age installed
#  - SOPS_AGE_KEY_FILE exported
#  - bubble-vps-data has a real secrets.sops.env for bubble-internal
#  - {{OPERATOR}} rotated the keys (we never re-encrypt the leaked ones)

# 1. Run the secrets task
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/secrets/deploy.py

# 2. Verify on the box
ssh hetzner '
    test -f /etc/age/key.txt && echo "✅ age key exists"
    test -f /etc/bubble/secrets.sops.env && echo "✅ encrypted file deployed"
    sudo sops --decrypt /etc/bubble/secrets.sops.env | grep -q OPENROUTER_API_KEY && echo "✅ decrypts cleanly with all required keys"
    [ "$(stat -c %a /etc/age/key.txt)" = "400" ] && echo "✅ age key mode 0400"
    [ "$(stat -c %a /etc/bubble/secrets.sops.env)" = "440" ] && echo "✅ encrypted file mode 0440"
'

# 3. Idempotency: re-run, expect zero changes
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/secrets/deploy.py
# parse Grand total — must show 0 Changed
```

### Cleanup test (the leaked-keys-must-be-gone validation)

After the secrets layer is live, run this to find any remaining plaintext leaks:

```bash
ssh hetzner '
    # The 8 known leak files from Step 1 audit:
    grep -l "sk-or-v1" /home/claude/.secrets /home/claude/start-claude-agent.sh 2>/dev/null && echo "❌ OR key still present" || echo "✅ no plaintext OR key in known files"
    grep -l "8350575119:AAH" /home/claude/.claude/channels/telegram/.env 2>/dev/null && echo "❌ bot token still present" || echo "✅ no plaintext bot token in known file"
'
```

Step 3 includes a CLEANUP task: `tasks/secrets/_cleanup_legacy.py` removes the 8 known plaintext copies after the encrypted layer is verified working. **NOT until verification — we don't lose the only working keys until we're sure the encrypted ones work.**

---

## Acceptance criteria for Step 3

Step 3 is DONE when:

1. ✅ sops + age installed on {{VPS_HOST}} (idempotent)
2. ✅ /etc/age/key.txt exists, mode 0400
3. ✅ secrets.sops.env in data repo (encrypted) is rsynced to /etc/bubble/secrets.sops.env
4. ✅ Box can decrypt it: `sudo sops --decrypt /etc/bubble/secrets.sops.env` works without errors
5. ✅ All `required_keys` from tenant.yaml are present in the decrypted output
6. ✅ Re-running the task is idempotent (zero changes)
7. ✅ The 8 known plaintext leak files have been deleted
8. ✅ Unit tests pass; integration dogfood test passes
9. ✅ deploy.py orchestration calls hardening THEN secrets THEN (later) agent install — order matters

---

## Open questions for Step 3 implementation

1. **Where does `SOPS_AGE_KEY_FILE` live on operator Mac?** Default: `~/.config/sops/age/keys.txt`. Document in INSTALL.md. Out of scope to manage via the platform repo (operator-side concern).

2. **`sops` binary install method** — apt has it but versions lag. Options: (a) apt (fastest, may be old), (b) direct GitHub release download (latest, more work). Recommendation: (b) for sops (rapid feature improvements matter), (a) for age (mature, apt version fine).

3. **Should the box's public age key be tracked in the data repo?** Recommendation: YES. After first-deploy bootstrap, commit `tenants/<name>/box-pubkey.txt` to the data repo. This makes recovery faster (we know which keypair the box was using) and audit-trail-friendlier. Tradeoff: minor metadata in git history.

4. **Cleanup of the 8 known plaintext files** — should this happen in the SAME deploy as secrets bring-up, or a separate manual step? Recommendation: SAME deploy, AFTER verification step 4 succeeds. If verification fails, cleanup doesn't run. We don't strand ourselves without working keys.

5. **What about the systemd unit?** Step 3 builds the secrets layer but doesn't yet wire it to a service. Step 4 (agent install) consumes it. Document this clearly so we don't leave {{OPERATOR}} confused that the agent still runs from the old plaintext config until Step 4.

---

## Cross-refs

- [Industry pattern guide — DCHost](https://www.dchost.com/blog/en/the-calm-way-to-secrets-on-a-vps-gitops-with-sops-age-systemd-magic-and-rotation-you-can-sleep-on/)
- [SOPS docs](https://getsops.io/docs/)
- [age GitHub](https://github.com/FiloSottile/age)
- `~/claude-workspaces/rnd/projects/hetzner-migration/CLAUDE_AGENT_RECIPE.md` — current plaintext-keys reality
- SPEC-005 — must be done before Step 3 (hardened box is a prereq)
