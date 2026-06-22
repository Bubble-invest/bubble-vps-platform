# RUNBOOK — Bubble VPS Platform

Operator playbook for the day-2 tasks: verifying the hardening playbook is
healthy, running the hardening task in isolation, and understanding what's
in and out of scope.

---

## How to verify the hardening playbook is healthy (drift test)

The Step-2 contract is: **running the hardening task against an already-hardened
host reports zero changes.** Any change reported is drift between the playbook
and reality.

### The dogfood test (automated)

```bash
cd ~/claude-workspaces/rnd/projects/bubble-vps-platform
tests/integration/test_dogfood_hardening.sh
```

The script does TWO runs in sequence:

1. **Run 1**: applies the hardening profile from
   `pyinfra/tasks/hardening/linux.py` against the `bubble-internal` tenant
   ({{VPS_HOST}}). Asserts pyinfra reports `Changed: 0` and `Errors: 0`.
2. **Run 2**: re-runs to confirm idempotency. Asserts the same.

Logs are written to:

- `/tmp/hardening-run1.log`, `/tmp/hardening-run2.log` (full pyinfra output)
- `/tmp/step2-dogfood-run1.log`, `/tmp/step2-dogfood-run2.log` (mirror,
  parent agent reads these)

### What "changed" means

`pyinfra` counts a change for any operation that actually had to run a command
on the remote. `apt.packages(present=True)` is a no-op (zero change) when the
package is already installed. `files.template` is a no-op when the rendered
content matches the remote file's hash. `server.shell` ALWAYS counts as a
change once executed — so we only emit `server.shell` ops behind explicit
guards (either `_if=op.did_change` or a fact-based Python `if` block).

### Negative test (drift detection)

After the dogfood passes, you can deliberately introduce drift to verify the
playbook DETECTS and CORRECTS it:

```bash
# 1) Manually break ufw on the box
ssh hetzner 'sudo ufw disable'

# 2) Re-run hardening — pyinfra should report 1 change (ufw re-enabled)
cd ~/claude-workspaces/rnd/projects/bubble-vps-platform
TENANT=bubble-internal ./.venv/bin/pyinfra inventory.py pyinfra/tasks/hardening/linux.py

# 3) Verify cleanup: re-run again — should be back to zero changes
TENANT=bubble-internal ./.venv/bin/pyinfra inventory.py pyinfra/tasks/hardening/linux.py
```

A similar drill works for any of the in-scope items (delete `/etc/fail2ban/jail.local`,
disable chrony, edit the swap sysctl, etc).

---

## What the hardening task does and does not manage

### In scope (managed)

| # | Item | How |
|---|------|-----|
| 1 | sshd config drop-in `/etc/ssh/sshd_config.d/00-bubble-hardening.conf` | jinja2 template, validated with `sshd -t -f` before reload |
| 2 | UFW (ufw package, default deny incoming, allow outgoing, `limit 22/tcp`, daemon enabled) | apt + idempotent `server.shell` guarded by `ufw status verbose` parsing |
| 3 | fail2ban `/etc/fail2ban/jail.local` (sshd jail mode=aggressive, recidive jail) | apt + jinja2 template + `restarted=True` only when file changed |
| 4 | unattended-upgrades `/etc/apt/apt.conf.d/52unattended-upgrades-bubble` (security-only origins, auto-reboot 04:00 UTC) | apt + jinja2 template |
| 5 | Swap (2 GB swapfile, `/etc/fstab` entry, `vm.swappiness=10` in `/etc/sysctl.d/99-bubble-swap.conf`) | shell guarded by `host.get_fact(File)`, `files.line` for fstab, `files.put` for sysctl |
| 6 | NTP (chrony installed, daemon enabled) | apt + systemd |

### Out of scope (managed elsewhere or not at all)

- **Hetzner Cloud Firewall** (hypervisor layer) — needs the `hcloud` API. Will land in Step 7 (provisioning task).
- **Non-root user creation** — bootstrap concern. We assume the box is provisioned with the configured `host.ssh_user` already present and the operator's pubkey in `~/.ssh/authorized_keys`.
- **SSH key deployment** — same.
- **Disk encryption / LUKS** — not applied on {{VPS_HOST}}; would have to be done at install time.
- **AppArmor / SELinux profiles** — Ubuntu 24.04 ships AppArmor enabled; we leave defaults.
- **Auditd** — overkill for our scale. Add if a client requires SOC2/ISO27001.

---

## How to run hardening only (without the deploy.py orchestration)

To apply just the hardening task — useful for tight iteration on the playbook
or for narrowing the dogfood test:

```bash
cd ~/claude-workspaces/rnd/projects/bubble-vps-platform
TENANT=bubble-internal ./.venv/bin/pyinfra inventory.py pyinfra/tasks/hardening/linux.py
```

This skips the `deploy.py` hello-message at the end. To run the whole orchestration
(hardening + hello), use the wrapper:

```bash
./scripts/deploy.sh --tenant=bubble-internal
```

To dry-run (parses tenant.yaml, plans operations, but does not connect):

```bash
TENANT=bubble-internal ./.venv/bin/pyinfra --dry-run inventory.py deploy.py
```

---

## Drift discoveries on {{VPS_HOST}} (2026-05-08, Step 2)

Our manual hardening on 2026-05-06 produced specific files that the playbook
must match byte-for-byte to pass the dogfood test. Where SPEC-005 differed
from reality, the implementation followed reality:

| Item | Spec said | Reality | Resolved |
|------|-----------|---------|----------|
| sshd file path | `/etc/ssh/sshd_config.d/99-bubble.conf` | `00-bubble-hardening.conf` | Use `00-bubble-hardening.conf` (intentional load-order before cloud-init's 50-) |
| sshd directives | 5 lines | 8 lines (+`KbdInteractive…`, +`ChallengeResponse…`, +`LoginGraceTime`) | Match reality |
| fail2ban file | `/etc/fail2ban/jail.d/bubble.conf` | `/etc/fail2ban/jail.local` | Match reality |
| sysctl file | `/etc/sysctl.d/99-bubble-platform.conf` | `/etc/sysctl.d/99-bubble-swap.conf` | Match reality, content `vm.swappiness=10` (no spaces) |
| unattended-upgrades content | 4 lines | 11 lines (extended with Allowed-Origins, Remove-Unused-*, etc) | Match reality |
| chrony | Add it | Not installed (timesyncd active) | Install — first run reports 1 change |

These are documented in each sub-module's docstring (`pyinfra/tasks/hardening/_*.py`).

---

## Troubleshooting

### "pyinfra reports N > 0 changes against a clean {{VPS_HOST}}"

You have drift. Either:
1. Someone manually edited the box (verify with `ssh hetzner 'sudo cat <file>'`)
2. The playbook itself drifted (compare the template + golden file to what's on the box)

To resolve: SSH to the box, inspect the actual state, then update either the
template/golden file (if the box is "right") or re-run the playbook (if the
playbook is "right"). The dogfood test is the litmus.

### "I get 'Permission denied (publickey)'"

The deploy uses key-based SSH from the operator's Mac to `claude@<ip>`. Verify:

```bash
ssh hetzner 'whoami; hostname'
# Expect: claude / {{VPS_HOST}}
```

If that works, pyinfra should too. If not, check `~/.ssh/config` has the
`hetzner` alias mapping to `claude@{{VPS_IP}}`.

### "UFW rate-limit blocks my deploy mid-run"

UFW limits SSH to 6 conns / 30 s / IP. pyinfra opens many short SSH connections
during a deploy. SPEC-004's mitigation: `scripts/deploy.sh` passes
`--retry 2 --retry-delay 5` by default. If you still hit it, just wait 30s and
re-run.

---

## Telegram plugin not responding

Symptom: bot reports `active` from systemd's POV but messages are silently
ignored. We hit this exact failure mode three times during the Step 4 → 5a
debugging on 2026-05-09. The `telegram-watchdog` cron ([SPEC-013](../specs/SPEC-013-telegram-recovery-watchdog.md))
runs every 5 min and auto-recovers most cases — but if you're investigating
manually:

```bash
ssh <tenant>-vps

# 1. Check the systemd-supervised service is up
sudo systemctl status claude-agent-morty.service

# 2. Check the watchdog's recent activity (it kicks the service when bot is broken)
sudo journalctl -t telegram-watchdog --since "1h ago"

# 3. Check the plugin's bot.pid (the watchdog's primary signal)
ls -la /home/claude/.claude/channels/telegram/bot.pid
sudo -u claude cat /home/claude/.claude/channels/telegram/bot.pid
sudo kill -0 $(sudo -u claude cat /home/claude/.claude/channels/telegram/bot.pid) && echo alive

# 4. Check the bun child process
sudo -u claude pgrep -af 'bun run.*telegram'

# 5. Manual restart if everything looks dead
sudo systemctl restart claude-agent-morty.service
sleep 15
ls -la /home/claude/.claude/channels/telegram/bot.pid    # should reappear
```

If the watchdog itself has been failing repeatedly, check
`/run/telegram-watchdog/last-restart` and the cooldown logic — see SPEC-013
§"Recovery action".

---

## Dashboard not loading

Symptom: `http://{{VPS_HOST}}.{{TAILNET}}.ts.net:3848/` does not load from a
tailnet device.

```bash
# 1. From operator Mac — verify Tailscale is up and you're talking to the right node
tailscale status | grep {{VPS_HOST}}

# 2. From operator Mac — check the dashboard port responds
curl -sS -o /dev/null -w "%{http_code}\n" http://{{VPS_HOST}}.{{TAILNET}}.ts.net:3848/

# 3. SSH to the box and check the service
ssh {{VPS_HOST}}
sudo systemctl status bubble-dashboard.service
sudo journalctl -u bubble-dashboard.service --since "1h ago" | tail -50

# 4. Check the BIND_ADDR (must be the Tailscale IP, NOT 0.0.0.0 — public exposure not allowed)
sudo systemctl cat bubble-dashboard.service | grep -E "Environment|BIND"

# 5. Verify the Tailscale interface holds the bind IP
ip -4 addr show tailscale0
```

The dashboard MUST bind to the Tailscale interface only (per [SPEC-015](../specs/SPEC-015-phone-home-dashboard.md))
— never `0.0.0.0`. If the bind address ever drifts to `0.0.0.0`, the
security-audit cron will flag it the next morning.

---

## Deploy fails at sops verify

Symptom: `pyinfra` aborts at the `tasks/secrets/_sops_deploy.py` validation
step with "could not decrypt" or "required key X missing".

Root causes (in likelihood order):

1. **Box pubkey not in `.sops.yaml`** — typical on first deploy if Phase D
   first-half gate was skipped. Fix:
   ```bash
   $EDITOR ../bubble-vps-data/.sops.yaml
   # Add the box's pubkey (printed during the first deploy) to the tenant's recipient block
   cd ../bubble-vps-data
   SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops updatekeys --yes tenants/<name>/secrets.sops.env
   git commit -am "Add <name> box pubkey to .sops.yaml"
   ```
   Then redeploy.

2. **Required key missing from secrets file.** The validator checks every entry
   in `cfg.secrets.required_keys` exists in the decrypted output. Fix:
   ```bash
   ./scripts/operator-set-secret.sh --tenant=<name> --key=<MISSING_KEY>
   ```
   Then redeploy. If the key shouldn't be required, remove it from
   `tenant.yaml`'s `secrets.required_keys` list instead.

3. **Box's age key was regenerated** (e.g. someone ran the bootstrap twice
   manually, or the `/etc/age/key.txt` was deleted and re-created). The pubkey
   in `.sops.yaml` no longer matches. Fix: replace the old pubkey in
   `.sops.yaml` with the new one (`ssh <name>-vps 'sudo cat /etc/age/key.pub'`),
   re-run `sops updatekeys`, redeploy.

The validator NEVER prints the decrypted plaintext. See [SPEC-008](../specs/SPEC-008-secrets-deploy-second-half.md) §"Hard rule" for why.

---

## Auto-restart loop

Symptom: `systemctl status claude-agent-morty.service` shows `Restart=on-failure`
firing repeatedly; uptime is always seconds.

```bash
ssh <tenant>-vps

# 1. Get the last 200 lines of the unit's journal
sudo journalctl -u claude-agent-morty.service --no-pager -n 200

# 2. Look for the failing ExecStartPre or ExecStart
sudo journalctl -u claude-agent-morty.service --no-pager -n 200 | grep -iE "exec|fail|error"

# 3. Common causes (from the 2026-05-09 debugging session):
#    - bun PATH issue: ExecStart can't find bun → check PATH in the unit file matches /home/claude/.bun/bin
#    - systemd quoting: --argument="value with spaces" needs careful escaping
#      (use systemd's environment expansion instead of shell quoting)
#    - skipDangerousModePermissionPrompt: if claude config schema changed,
#      the unit's --settings flag may pass an outdated key
#    - sops decrypt failure: ExecStartPre fails → see "Deploy fails at sops verify" above
#    - missing /run/claude-agent: tmpfs not mounted → check
#      sudo systemctl cat claude-agent-morty.service | grep -i runtime
```

The pattern from the debugging session: each restart runs ExecStartPre
(decrypt to tmpfs), then ExecStart (claude). If ExecStartPre succeeds but
ExecStart dies in <5s, the unit hits Restart=on-failure and loops.

Mitigation: add `RestartSec=10s` and `StartLimitBurst=5` to the unit so it
doesn't burn CPU thrashing — but the real fix is to find the root cause in
the journal. NEVER mask the failure with `Restart=always`.

---

## Agent startup failures (debugging the verification gate)

Symptom: a deploy **aborts at the agent verification gate** (`pyinfra` stops at
`tasks/agent/_verify.py` with a non-zero `server.shell` op), OR a freshly
deployed `claude-agent-<persona>.service` never reaches a healthy state.

The gate (`pyinfra/tasks/agent/_verify.py`) runs SIX checks PER concierge AFTER
the unit is started but BEFORE `_cleanup_legacy` removes the plaintext fallback
— so a failed gate is non-destructive (the rollback path stays intact). When the
gate aborts, pyinfra prints which named op failed; reproduce it by hand on the
box to find the root cause. For a primary concierge (e.g. morty) the runtime env
file is `/run/claude-agent/env`; for additional concierges it is
`/run/claude-agent-<name>/env`.

```bash
ssh <tenant>-vps
SVC=claude-agent-morty.service          # adjust <persona>
ENV=/run/claude-agent/env               # /run/claude-agent-<name>/env for non-primary

# Gate check 1 — service ACTIVE
sudo systemctl is-active --quiet "$SVC" && echo "1 OK: active" || echo "1 FAIL: not active"

# Gate check 2 — service ENABLED (survives reboot)
sudo systemctl is-enabled --quiet "$SVC" && echo "2 OK: enabled" || echo "2 FAIL: not enabled"

# Gate check 3 — decrypted runtime env present, mode 0400, owned by claude
sudo test -f "$ENV" && echo "3a OK: env present" || echo "3a FAIL: env missing (sops decrypt ExecStartPre failed)"
sudo sh -c "[ \"\$(stat -c %a $ENV)\" = 400 ]" && echo "3b OK: mode 0400" || echo "3b FAIL: wrong mode"
sudo sh -c "[ \"\$(stat -c %U $ENV)\" = claude ]" && echo "3c OK: owned by claude" || echo "3c FAIL: wrong owner"

# Gate check 4 — unit DECLARES EnvironmentFile=<env>
sudo systemctl show "$SVC" --property=EnvironmentFiles | grep -q "$ENV" \
    && echo "4 OK: EnvironmentFile declared" || echo "4 FAIL: unit missing EnvironmentFile"

# Gate check 5 — expected env-var NAMES present in the decrypted env file
#   (NAMES only — never values; the file is mode 0400 so this needs sudo).
#   Primary expects CLAUDE_CODE_OAUTH_TOKEN + TELEGRAM_BOT_TOKEN; a non-primary
#   concierge's own <REF> is REMAPPED to TELEGRAM_BOT_TOKEN in ITS env file.
for k in CLAUDE_CODE_OAUTH_TOKEN TELEGRAM_BOT_TOKEN; do
    sudo grep -q "^$k=" "$ENV" && echo "5 OK: $k present" || echo "5 FAIL: $k missing from env"
done

# Gate check 6 — no error-priority journal lines since startup
sudo journalctl -u "$SVC" --since "30 seconds ago" --priority=err --no-pager | tail -20
# (empty output = check 6 passes)
```

Interpreting failures:

| Failing check | Most likely cause | Where to look |
|---------------|-------------------|---------------|
| 1 (not active) / 6 (errors) | ExecStart died — bun PATH, model alias, quoting | `sudo journalctl -u "$SVC" -n 200` → see "Auto-restart loop" |
| 3a (env missing) | the `sops --decrypt` ExecStartPre failed | "Deploy fails at sops verify" section above |
| 3b/3c (mode/owner) | the chown/chmod ExecStartPre chain regressed | `sudo systemctl cat "$SVC" \| grep ExecStartPre` |
| 4 (no EnvironmentFile) | unit-template regression | re-render: compare `sudo systemctl cat "$SVC"` to the golden unit |
| 5 (var missing) | required key absent from `secrets.sops.env` | "Deploy fails at sops verify" → `operator-set-secret.sh` |

The gate NEVER echoes a credential value. If you must inspect the env layout,
`sudo cut -d= -f1 "$ENV"` prints only the KEY NAMES.

---

## Secret needs rotation

Routine: e.g. an OpenRouter key or Telegram bot token has been suspected
leaked or hit its rotation deadline.

**Safe rotation discipline: re-encrypt → re-deploy → verify.** Never edit the
ciphertext by hand, never SSH a new value onto the box directly, and always
confirm the gate passed after the redeploy. The three phases:

```bash
# ─── Phase 1: re-encrypt (operator Mac) ────────────────────────────────────
# 1. Update the encrypted secrets file via the GUI prompt. This re-encrypts
#    the WHOLE secrets.sops.env to the tenant's recipients (operator master +
#    box pubkey) with the new value substituted — the value never echoes.
./scripts/operator-set-secret.sh --tenant=<name> --key=<KEY_NAME>

# 1b. Confirm the new ciphertext still decrypts for the operator (sanity — does
#     NOT print plaintext, only exit code) and the key is present:
cd ../bubble-vps-data
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \
    sops --decrypt tenants/<name>/secrets.sops.env | cut -d= -f1 | grep -qx '<KEY_NAME>' \
    && echo "re-encrypt OK: <KEY_NAME> present" || echo "FAIL: key missing after re-encrypt"

# 2. Commit the change
git commit -am "Rotate <KEY_NAME> for <name>"
cd -

# ─── Phase 2: re-deploy (operator Mac) ─────────────────────────────────────
# 3. Redeploy. files.put on /etc/bubble/secrets.sops.env detects the changed
#    hash and triggers `systemctl restart claude-agent-<persona>.service`
#    (SPEC-012). The agent verification gate (see "Agent startup failures"
#    above) then re-runs — a green deploy already proves the new secret loaded.
./scripts/deploy.sh --tenant=<name>

# ─── Phase 3: verify on the box ────────────────────────────────────────────
# 4. Confirm the new ciphertext decrypted into the runtime env (NAMES only):
ssh <name>-vps 'sudo cut -d= -f1 /run/claude-agent/env | grep -x <KEY_NAME> \
    && echo "verify OK: <KEY_NAME> in runtime env" || echo "FAIL: key absent"'

# 5. Confirm the service came back healthy after its restart:
ssh <name>-vps 'sudo systemctl is-active --quiet claude-agent-<persona>.service \
    && echo active || echo FAIL'

# 6. Functional smoke test (per rotated key):
#    - TELEGRAM_BOT_TOKEN → DM the bot, expect a reply. If the poller wedged on
#      the old token, restart it: ssh <name>-vps 'sudo systemctl restart claude-agent-<persona>.service'
#    - TAILSCALE_AUTHKEY → the box only re-auths on `tailscale up`, which only
#      runs when not already authenticated; rotating the authkey matters for
#      NEW boxes, not an already-joined node.
#    - PHONEHOME_TOKEN → confirm the next heartbeat is accepted (dashboard
#      shows a fresh timestamp within `phone_home.interval_minutes`).
```

If Phase 3 step 4 shows the key ABSENT from the runtime env, the redeploy did
not pick up the change — most often because the commit in Phase 1 was skipped or
the deploy targeted the wrong tenant. Re-run Phase 2 and recheck; the plaintext
fallback is never touched, so this is safe to retry.

The "restart on secret change" hook is per [SPEC-012](../specs/SPEC-012-secrets-restart-on-change.md)
— `files.put` on `/etc/bubble/secrets.sops.env` triggers
`systemctl restart claude-agent-<persona>.service` only when the file's hash
actually changed.

For multi-tenant rotation (e.g. a shared OpenRouter key compromised across
all tenants), there's no batch script yet — loop manually:

```bash
for tenant in bubble-internal acme-corp widgets-inc; do
    ./scripts/operator-set-secret.sh --tenant=$tenant --key=OPENROUTER_API_KEY
    ./scripts/deploy.sh --tenant=$tenant
done
```

Future: `scripts/rotate-secrets.sh --secret=<KEY> --new=<value> --all` per
the end-state vision (not in Phase 1).

---

## Sandbox violations (AppArmor denials, bwrap failures)

The OS sandbox (Layer B — anti prompt-injection) is installed by
`pyinfra/tasks/hardening/_sandbox.py`, which delegates to
`bubble-ops-loop/scripts/install-sandbox.sh`. It installs **bwrap**
(bubblewrap), **socat**, an **AppArmor** profile, and
`@anthropic-ai/sandbox-runtime`, and merges a `sandbox` block into
`/etc/claude-code/managed-settings.json`. Symptoms of trouble: the agent's Bash
tool reports `Permission denied` / `Operation not permitted` on paths that
should be allowed, child tools fail to spawn, or the journal shows `apparmor`
`DENIED` lines.

### Triage: is the sandbox even engaged?

```bash
ssh <tenant>-vps

# 1. Sandbox runtime + jail tooling installed?
which bwrap socat                         # both must resolve
bwrap --version                           # sanity: bwrap runs at all

# 2. managed-settings declares the sandbox block?
sudo cat /etc/claude-code/managed-settings.json | python3 -m json.tool | grep -iA3 sandbox

# 3. AppArmor enabled + the claude/bwrap profile loaded?
sudo aa-status | grep -iE "profiles are loaded|profiles are in enforce"
sudo aa-status | grep -iE "claude|bwrap|sandbox"
```

If `aa-status` shows the profile in **complain** mode (not **enforce**), denials
are LOGGED but not blocked — useful while diagnosing, but it is not the
production posture.

### Reading AppArmor denials

AppArmor logs every block to the kernel audit log. Each `DENIED` line names the
profile, the operation, and the exact path/capability that was refused:

```bash
# Recent denials (kernel audit via journald)
sudo journalctl -k --since "30 min ago" | grep -i 'apparmor="DENIED"'

# Or straight from the audit log if auditd is present
sudo grep 'apparmor="DENIED"' /var/log/audit/audit.log 2>/dev/null | tail -30

# Or dmesg for the most recent
sudo dmesg | grep -i 'apparmor.*DENIED' | tail -30
```

A denial line looks like:
`apparmor="DENIED" operation="open" profile="..." name="/path/it/wanted" ...`
— the `name=` is the resource the agent was refused, the `operation=` is what it
tried (open/exec/mount/etc). That tells you whether the profile is too tight
(legitimate path blocked → the profile needs the path added upstream in
`install-sandbox.sh`) or whether the agent genuinely tried something it should
not (prompt-injection working as intended — do NOT loosen the profile; this is
the layer doing its job).

### bwrap (bubblewrap) failures

bwrap needs unprivileged user namespaces. If the jail fails to construct, the
agent's sandboxed Bash fails before the command even runs:

```bash
# 1. Unprivileged user namespaces enabled? (must print 1)
sudo sysctl kernel.unprivileged_userns_clone 2>/dev/null
cat /proc/sys/user/max_user_namespaces        # must be > 0

# 2. Reproduce a minimal bwrap jail as the claude user
sudo -u claude bwrap --ro-bind / / --dev /dev --proc /proc echo "bwrap OK"
#    "bwrap OK"             → jail constructs fine; the issue is profile/policy
#    "Creating new namespace failed: Operation not permitted"
#                           → userns disabled at the kernel/sysctl level

# 3. Inspect what the sandbox runtime is invoking
sudo -u claude pgrep -af bwrap                 # see the live jail args
```

Common bwrap root causes:

| Error | Cause | Fix |
|-------|-------|-----|
| `Creating new namespace failed: Operation not permitted` | unprivileged userns disabled | `sudo sysctl -w kernel.unprivileged_userns_clone=1` (persist in `/etc/sysctl.d/`); verify the host/provider allows userns |
| `bwrap: ... No such file or directory` on a `--bind` path | a path the runtime expected is absent | re-run the sandbox install (idempotent): `sudo bash /home/claude/bubble-ops-loop/scripts/install-sandbox.sh` |
| Bash works but writes are refused | AppArmor profile (not bwrap) is enforcing | read the `DENIED` line — see "Reading AppArmor denials" above |

### Re-applying the sandbox

The install is idempotent and safe to re-run. Re-apply via pyinfra (preferred —
keeps it in the managed flow) or directly on the box:

```bash
# Preferred: through the hardening task (re-runs _sandbox.apply())
TENANT=<name> ./.venv/bin/pyinfra inventory.py pyinfra/tasks/hardening/linux.py

# Direct (on the box) — the same script pyinfra delegates to
ssh <tenant>-vps 'sudo bash /home/claude/bubble-ops-loop/scripts/install-sandbox.sh'

# After re-applying, reload AppArmor profiles and re-check status
ssh <tenant>-vps 'sudo systemctl reload apparmor && sudo aa-status | grep -i enforce'
```

The morning **security-audit cron** ([SPEC-014](../specs/SPEC-014-cloud-security-cron.md))
also reports sandbox/AppArmor posture — check its last run if you suspect the
sandbox silently disengaged overnight.
