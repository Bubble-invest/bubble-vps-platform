# SPEC-008 — Step 3 Phase D second-half: secrets deploy + on-box validation

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending Joris approval_
**Depends on:** SPEC-006, SPEC-007, Phase D first-half (sops/age installed, box keypair generated, both recipients in `.sops.yaml`, secrets.sops.env updated via `sops updatekeys`)
**Implements:** the second half of Step 3 of the Bubble VPS Platform build plan

---

## Purpose

Now that the box has its own age private key AND is a recipient on `secrets.sops.env`, this task ships the encrypted file to the box and verifies the box can decrypt it cleanly with its own key. After this, the secrets layer is operationally complete (Step 4 wires the agent service to consume it).

---

## Scope

In:
- pyinfra task `tasks/secrets/_sops_deploy.py` that:
  1. Ensures `/etc/bubble/` exists (mode 0755, root:root)
  2. Uploads `bubble-vps-data/tenants/<name>/secrets.sops.env` → `/etc/bubble/secrets.sops.env` (mode 0440, root:root). Use `pyinfra.operations.files.put` with `force=False` so it only re-uploads if file content differs.
  3. Test-decrypts on the box via `sops --decrypt`. Captures exit code only (or length-of-decrypted-text). NEVER prints plaintext.
  4. Validates `required_keys` from `cfg.secrets.required_keys` are all present in the decrypted output. Done via `grep -q "^${KEY}="` on the decrypted output piped to /dev/null.
  5. If validation fails: aborts the deploy with a clear error.
- `tasks/secrets/deploy.py` orchestration calls `_sops_deploy` after `_age_setup`.

Out (deferred to Step 4):
- systemd unit / EnvironmentFile wiring
- Cleanup of legacy plaintext files (the 8 known leaks)
- Restart of any service (no service yet consumes /etc/bubble/secrets.sops.env)

---

## Hard rule (the lesson from 2026-05-08)

**No `sops --decrypt` output ever reaches pyinfra's stdout/stderr** — and therefore never reaches operator transcripts.

Patterns ALLOWED:
```bash
# Exit code check
sops --decrypt /etc/bubble/secrets.sops.env > /dev/null && echo "decrypt-ok"

# Length-only signature
sops --decrypt /etc/bubble/secrets.sops.env | wc -l

# Required-key existence check (one grep per key)
sops --decrypt /etc/bubble/secrets.sops.env | grep -q "^TELEGRAM_BOT_TOKEN=" && echo "key-present"
```

Patterns FORBIDDEN:
```bash
sops --decrypt FILE                      # echoes all values
sops --decrypt FILE | head                # echoes first values
sops --decrypt FILE > /tmp/out            # WORSE — persists to disk
cat /run/<service>/env                    # leaks decrypted env
echo $DECRYPTED                           # echoes a bash variable holding plaintext
```

If a sub-step needs the plaintext briefly, it goes to a tmpfs path with mode 0400 root-only, used by a subprocess, then `shred`/`rm`. That's reserved for systemd ExecStartPre at Step 4 — NOT for our test-decrypt verification at Step 3.

---

## Idempotency

- `files.directory(/etc/bubble)`: idempotent (pyinfra default)
- `files.put(secrets.sops.env)`: pyinfra hashes both sides; only uploads if different
- Test-decrypt: always runs (it's a verification, not a state mutation; reports "Success" each time but produces no diff)
- Required-key validation: same — verification, not mutation

Acceptable result on a clean re-run: ~0 changes for the file ops, 1-2 verification operations report "Success" (no-change behavior matches Step 2's pattern).

---

## File modes + ownership

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/etc/bubble/` | root:root | 0755 | Bubble platform config root |
| `/etc/bubble/secrets.sops.env` | root:root | 0440 | Encrypted blob — read-only group, world-no-access |

Note: 0440 means root + members of root group can read, others cannot. Sufficient because:
- The age private key (`/etc/age/key.txt`, 0400) is the actual decryption gate; the encrypted blob being slightly more readable doesn't reduce security
- 0440 lets a future systemd ExecStartPre (running as root with group root) read it without `sudo`

If we wanted defense-in-depth, 0400 root:root works equivalently. Use 0440 to leave room for read-only group membership later.

---

## Tasks layout (additions)

```
bubble-vps-platform/pyinfra/tasks/secrets/
├── __init__.py                     # (existing)
├── _binaries.py                    # (existing — Phase D first half)
├── _age_setup.py                   # (existing — Phase D first half)
├── _sops_deploy.py                 # NEW — this spec
└── deploy.py                       # UPDATED — add _sops_deploy.apply() call
```

---

## Test plan

### Unit tests
- `test_sops_deploy_required_keys_validation()` — given a fake decrypted env content, validator catches missing keys
- `test_sops_deploy_no_plaintext_in_pyinfra_logs()` — render the operations module, grep for `sops --decrypt$` (without /dev/null or grep -q) — must not find such patterns. Static check that the implementation follows the masking rule.

### Integration test
- After deploy: `ssh hetzner 'sudo test -f /etc/bubble/secrets.sops.env'` exit 0
- `ssh hetzner 'sudo stat -c %a /etc/bubble/secrets.sops.env'` returns `440`
- `ssh hetzner 'sudo SOPS_AGE_KEY_FILE=/etc/age/key.txt /usr/local/bin/sops --decrypt /etc/bubble/secrets.sops.env > /dev/null && echo OK'` returns `OK` (exit code only)
- Re-run deploy: zero file-mutation changes for the secrets section

### Negative test (manual, optional)
After deploy succeeds, deliberately corrupt the on-box file:
```bash
ssh hetzner 'echo "garbage" | sudo tee -a /etc/bubble/secrets.sops.env'
```
Re-run deploy: pyinfra should detect the hash change, re-upload, and verify decryption (1 file change, validation still passes because we re-shipped the good file).

---

## Acceptance criteria

Step 3 is DONE when:
1. ✅ `/etc/bubble/secrets.sops.env` exists on box, mode 0440 root:root
2. ✅ `sops --decrypt` succeeds on box using `/etc/age/key.txt`
3. ✅ All `required_keys` from tenant.yaml present in decrypted output (verified via grep -q)
4. ✅ Re-running the task is idempotent (zero file mutations)
5. ✅ NO plaintext value appears in any pyinfra stdout/stderr
6. ✅ Test suite passes (45/45 + new tests = 47-50)
7. ✅ Step 2 hardening still 18/18 No-change (no regression)

---

## Out of scope

- systemd service installation (Step 4)
- Plaintext leak file cleanup (Step 4)
- Rotation workflow documentation (already in SPEC-006)
- Multi-tenant deploy paths (deferred until Tenant #2 exists)
