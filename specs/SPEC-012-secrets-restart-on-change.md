# SPEC-012 — Secrets file change → agent restart trigger (Task A)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 1-6a done; existing `_settings.py` restart-on-change pattern
**Implements:** Task A of the post-Step-6a follow-up batch

---

## Problem

When `bubble-vps-data/tenants/<name>/secrets.sops.env` is updated (operator added a new key, rotated a value, etc.), `pyinfra/tasks/secrets/_sops_deploy.py` rsyncs the new encrypted file to `/etc/bubble/secrets.sops.env` on the box. The systemd unit's `ExecStartPre=sops --decrypt` would pick up the change ON RESTART — but **nothing currently triggers a restart**. The agent keeps running with its OLD `/run/claude-agent/env` (decrypted at last service start) until the next operator-driven restart.

This bit us during Step 6a: added `TAILSCALE_AUTHKEY` to `secrets.sops.env`, deployed, the encrypted file landed on the box, but `_verify` failed because the agent's runtime env didn't have the new key (it was decrypted before TAILSCALE_AUTHKEY existed). Manual `systemctl restart` resolved it; this spec automates that.

---

## Pattern reference

Already in `_settings.py` (added 2026-05-08):
```python
settings_op = files.template(...)
server.shell(
    name=f"agent/settings: restart {service_name} if settings.json changed",
    commands=[
        f"systemctl list-unit-files {service_name} >/dev/null 2>&1 && "
        f"systemctl restart {service_name} || "
        f"echo 'service not yet installed — first-deploy path'"
    ],
    _if=settings_op.did_change,
    _sudo=True,
)
```

Same shape needed in `_sops_deploy.py`: capture the rsync op, restart the agent service if the file changed.

---

## Implementation

In `pyinfra/tasks/secrets/_sops_deploy.py`:

1. Capture the file-upload op into a variable: `upload_op = files.put(...)`
2. After the verify steps, add a new server.shell that restarts the agent service ONLY if `upload_op.did_change` AND the service exists.
3. **Important:** the agent service name is `claude-agent-<persona>.service` — `_sops_deploy` doesn't currently know the persona name. It needs to read `cfg.agent.persona.name` (already accessible via `get_tenant_config(host)`).
4. **Edge case (cold deploy):** on first-ever deploy, `_sops_deploy` runs BEFORE `_systemd_unit` creates the service. The restart command must tolerate "service not yet installed" — same pattern `_settings.py` uses (the `systemctl list-unit-files <name> >/dev/null 2>&1 && systemctl restart || echo` chain).
5. **Edge case (verify ordering):** `_sops_deploy` currently has these ops in order:
   - ensure /etc/bubble exists
   - upload encrypted file
   - verify on-box decryption (exit code only)
   - verify required keys present (one grep -q per key)
   The restart should run AFTER the verify ops, not before. If verify fails on the OLD env, that's a real signal — better to fail-stop than restart-and-hope. Wait — actually: on first deploy of a new key, verify checks the ENCRYPTED file's decryption (good — sops decrypts the new file directly), it does NOT check `/run/claude-agent/env`. So verify will pass even with stale env. The restart should come AFTER verify-on-encrypted-file passes; the restart then refreshes the runtime env so the agent layer's `_verify` (which DOES check `/run/claude-agent/env`) can see the new keys.

So order in `_sops_deploy`:
   1. ensure /etc/bubble
   2. upload encrypted file (capture op)
   3. verify encrypted file decrypts cleanly (always run, no diff)
   4. verify all required_keys present in the DECRYPTED OUTPUT (always run, no diff)
   5. **NEW**: if upload_op.did_change → restart agent service (so /run/claude-agent/env refreshes for the agent layer's later _verify ops)

---

## Idempotency

- Steady state (no secrets changes): upload_op.did_change is False → restart skipped → zero deploy mutations
- Secrets file changed: 1 mutation (the restart). Verify ops always re-run as Successes (by-design, not state mutations). Net mutation count: 1.
- Cold deploy (no service yet): upload_op.did_change is True (file is new) → restart command runs but the `systemctl list-unit-files` guard makes it a no-op (echoes the friendly message). Net behavioral change: zero.

---

## Test plan

### New tests in `lib/test_secrets_layer.py`

1. **`test_sops_deploy_module_has_restart_on_change`** — static check: grep `_sops_deploy.py` for the restart-shell-command pattern. Must find:
   - A `server.shell` with `name` containing "restart" AND containing the persona name templating
   - An `_if=upload_op.did_change` clause
   - A `_sudo=True` clause
   - The `list-unit-files` guard (to handle cold-deploy ordering)

2. **`test_sops_deploy_restart_uses_get_tenant_config_for_persona_name`** — static check: the restart command must reference `cfg.agent.persona.name` (not a hardcoded "morty" or "ricky") — proves it's tenant-portable.

3. **`test_sops_deploy_restart_command_no_unredirected_decrypt`** — extend the existing SPEC-008 hard-rule scan to cover the new `server.shell` in addition to the existing ops. Should pass naturally because the restart command doesn't decrypt anything.

### Integration test (manual, single-shot)

After deploy succeeds:
1. Edit `secrets.sops.env` to add a sentinel key (`TEST_SENTINEL=hello`)
2. Add `TEST_SENTINEL` to `required_keys` in tenant.yaml
3. Re-deploy
4. Expected: 1 mutation in the secrets section (the restart), 0 errors
5. Verify on box: `sudo grep -q '^TEST_SENTINEL=' /run/claude-agent/env && echo present`
6. Cleanup: revert tenant.yaml, edit `secrets.sops.env` to remove TEST_SENTINEL, re-deploy

(The integration test is OPTIONAL — the static tests + SPEC-006 acceptance criteria already cover the path. The manual test is for confidence on first run.)

---

## Acceptance criteria

Task A done when:
1. ✅ `_sops_deploy.py` has the new restart-on-upload-change op
2. ✅ Reads persona name from `cfg.agent.persona.name`
3. ✅ Tolerates cold-deploy ordering (no systemd unit yet)
4. ✅ 3 new static tests pass
5. ✅ Existing 106/106 tests still pass
6. ✅ pyinfra deploy idempotent (no spurious restarts on no-change runs)
7. ✅ deploy logs grep clean for credential prefixes

---

## Out of scope

- Deciding HOW to invalidate the agent's in-memory state if the change requires more than a process restart (e.g. CLAUDE_CODE_OAUTH_TOKEN rotation might require fresh auth handshake). For now, restart = sufficient. Real-world rotation testing is its own checklist.
- Restarting other tenant services (none exist yet — multi-agent is deferred).
