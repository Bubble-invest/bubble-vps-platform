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
`hetzner` alias mapping to `claude@178.105.77.178`.

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

Symptom: `http://{{VPS_HOST}}.tail<id>.ts.net:3848/` does not load from a
tailnet device.

```bash
# 1. From operator Mac — verify Tailscale is up and you're talking to the right node
tailscale status | grep {{VPS_HOST}}

# 2. From operator Mac — check the dashboard port responds
curl -sS -o /dev/null -w "%{http_code}\n" http://{{VPS_HOST}}.tail<id>.ts.net:3848/

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

## Secret needs rotation

Routine: e.g. an OpenRouter key or Telegram bot token has been suspected
leaked or hit its rotation deadline.

```bash
# 1. Operator Mac: update the encrypted secrets file via the GUI prompt
./scripts/operator-set-secret.sh --tenant=<name> --key=<KEY_NAME>
# (script opens a native password dialog; value never echoes)

# 2. Commit the change
cd ../bubble-vps-data
git commit -am "Rotate <KEY_NAME> for <name>"
cd -

# 3. Redeploy — the agent service will be restarted because /etc/bubble/secrets.sops.env changed
./scripts/deploy.sh --tenant=<name>

# 4. Smoke test
#    Telegram bot still answers (if you rotated TELEGRAM_BOT_TOKEN, you may
#    need to restart the bot's polling first)
#    Tailscale still up (if you rotated TAILSCALE_AUTHKEY — note: the box
#    only re-auths on `tailscale up`, which only runs if not already
#    authenticated; rotation matters only for new boxes)
```

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
