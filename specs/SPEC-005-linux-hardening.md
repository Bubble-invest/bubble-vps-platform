# SPEC-005 — Linux hardening task (Step 2)

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending Joris approval_
**Depends on:** SPEC-001 (tenant.yaml schema), SPEC-002 (inventory + deploy)
**Implements:** Step 2 of the Bubble VPS Platform build plan

---

## Purpose

Port the manual hardening done on `joris-cx33` (2026-05-06) into pyinfra
code. Make it **declarative + idempotent** so that:

1. Re-running against an already-hardened box reports **zero changes** ("dogfood validation")
2. Running against a fresh Ubuntu 24.04 box brings it to the same hardened state
3. Future drift (someone disables fail2ban, or `apt remove ufw`) is detected by re-running

The dogfood validation is the litmus test. If `pyinfra deploy` against `joris-cx33` reports a single change, we have drift between code and reality. Goal is **zero changes** on the dogfood run.

---

## Scope

### In scope (Step 2)

What pyinfra can manage from the operator's Mac via SSH:

| # | Hardening item | pyinfra mechanism |
|---|---|---|
| 1 | sshd config (`PermitRootLogin`, `PasswordAuthentication`, `MaxAuthTries`) | `files.line` / `files.put` for `/etc/ssh/sshd_config.d/99-bubble.conf` + `systemd.service(reloaded=True)` |
| 2 | UFW (default deny incoming, allow outgoing, SSH rate-limit) | `apt.packages(name='ufw')` + `server.shell` for ufw rules + `systemd.service('ufw', running=True, enabled=True)` |
| 3 | fail2ban (sshd jail aggressive + recidive jail) | `apt.packages(name='fail2ban')` + `files.put` for `/etc/fail2ban/jail.d/bubble.conf` + `systemd.service` |
| 4 | unattended-upgrades + auto-reboot 04:00 UTC | `apt.packages(name='unattended-upgrades')` + `files.put` for `/etc/apt/apt.conf.d/52unattended-upgrades-bubble` |
| 5 | Swap (2GB swapfile, swappiness=10, fstab persisted) | `server.shell` (idempotent guards) + `files.line` for `/etc/sysctl.d/99-bubble.conf` and `/etc/fstab` |
| 6 | NTP (chrony) — added since the original hardening had implicit `systemd-timesyncd` | `apt.packages(name='chrony')` + `systemd.service` |

### Out of scope

- **Hetzner Cloud Firewall** (item 7 in original hardening) — hypervisor-level, requires `hcloud` API. Defer to Step 7 (provisioning task).
- **Non-root user creation** (item 1) — bootstrap concern. v1 assumes the box is provisioned with the configured `host.ssh_user` already present. Document this as a Step 0 prerequisite.
- **SSH key deployment** — same; assume key is already authorized.
- **Disk encryption / LUKS** — not present on joris-cx33 (Hetzner CX33 doesn't ship encrypted-by-default). Add later if needed.
- **AppArmor / SELinux profiles** — Ubuntu 24.04 has AppArmor enabled by default; we leave defaults for v1.
- **Auditd** — overkill for our scale. Add later if a client requires SOC2/ISO27001.

---

## Configuration → tenant.yaml

The hardening task reads its config from `cfg.hardening` (already specified in SPEC-001). All fields are tenant-overridable. Per-field defaults match what's currently on `joris-cx33`.

```yaml
hardening:
  ufw:
    enabled: true
    allow_ssh_from: any        # or list of CIDRs
  fail2ban:
    enabled: true
    sshd_jail: aggressive
    bans:
      maxretry: 5
      findtime_minutes: 10
      bantime_hours: 1
  sshd:
    permit_root_login: "no"
    password_authentication: "no"
    max_auth_tries: 3
  unattended_upgrades:
    enabled: true
    auto_reboot_time: "04:00"  # UTC
  swap:
    enabled: true
    size_gb: 2
    swappiness: 10
  hetzner_cloud_firewall:
    enabled: true              # informational — Step 7 manages
    firewall_id: "10938002"
```

---

## Module layout

```
bubble-vps-platform/pyinfra/tasks/hardening/
├── __init__.py
├── linux.py                  ← entrypoint (called by deploy.py)
├── _sshd.py                  ← sshd config sub-module
├── _ufw.py                   ← UFW rules sub-module
├── _fail2ban.py              ← fail2ban jails sub-module
├── _unattended.py            ← unattended-upgrades sub-module
├── _swap.py                  ← swapfile sub-module
└── _ntp.py                   ← chrony sub-module
```

`linux.py` orchestrates by calling each sub-module's `apply(cfg)` function. Sub-modules are private (`_` prefix) — only `linux.py` is the public entrypoint.

---

## Public API

```python
# pyinfra/tasks/hardening/linux.py
from lib.host_helpers import get_tenant_config

def apply():
    """Apply the full Linux hardening profile to the current host.

    Reads cfg.hardening from tenant.yaml. Each sub-module is responsible
    for its own idempotency (re-runnable with zero diff if state matches).
    """
    cfg = get_tenant_config()
    h = cfg.hardening

    if h.sshd:
        _sshd.apply(h.sshd)
    if h.ufw and h.ufw.enabled:
        _ufw.apply(h.ufw)
    if h.fail2ban and h.fail2ban.enabled:
        _fail2ban.apply(h.fail2ban)
    if h.unattended_upgrades and h.unattended_upgrades.enabled:
        _unattended.apply(h.unattended_upgrades)
    if h.swap and h.swap.enabled:
        _swap.apply(h.swap)

    _ntp.apply()  # always — drift-free time is a basic requirement
```

---

## Idempotency rules

Each sub-module MUST satisfy:

1. **First run on a vanilla box:** brings the system to the desired state; pyinfra reports the changes
2. **Second run on the same box:** zero changes reported
3. **Run after manual drift:** detects + corrects (e.g. someone disabled UFW, we re-enable)

Specific patterns:

- **Config files:** use `files.put(src=..., dest=..., mode=..., user=root, group=root)` — pyinfra checks hash and only writes if different
- **Apt packages:** `apt.packages(name=..., present=True, update=False)` is idempotent by default (only installs if missing)
- **Services:** `systemd.service(name=..., running=True, enabled=True)` is idempotent
- **Sysctls:** write to `/etc/sysctl.d/99-bubble.conf` (one file, owned by us) + `server.shell(commands=['sysctl -p /etc/sysctl.d/99-bubble.conf'], _if=<config_changed>)` — only run sysctl reload if the file changed
- **fstab:** `files.line(path='/etc/fstab', line=..., present=True)` — adds line if absent, leaves alone if present
- **Swapfile creation:** guarded by `server.shell(commands=['test -f /swapfile || (fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile)'])` — only creates if missing

---

## Sub-module specifics

### _sshd.py

Manages a single drop-in file `/etc/ssh/sshd_config.d/99-bubble.conf`:

```
# Managed by bubble-vps-platform — do not edit manually.
PermitRootLogin no
PasswordAuthentication no
MaxAuthTries 3
PubkeyAuthentication yes
PermitEmptyPasswords no
```

Uses `files.template` (jinja2 from `templates/sshd_99-bubble.conf.j2`) so the values come from `cfg.sshd`.

After write, `systemd.service('ssh', reloaded=<file_changed>)` — only reloads sshd if the file changed.

**Guard:** never let a deploy lock us out. The reload happens AFTER pyinfra's connection is established; a config that breaks future logins still allows the in-flight session to complete the deploy. But a typo could brick the box. Mitigation:

- `_sshd.py` runs `sshd -t -f /etc/ssh/sshd_config.d/99-bubble.conf` BEFORE the reload (`server.shell` with name "Validate sshd config"). If validation fails, the operation errors and the reload is skipped. Manual recovery via Hetzner web console if it ever happens.

### _ufw.py

```
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp comment 'SSH (rate-limited: 6 conns/30s/IP)'
ufw --force enable
```

Each line wrapped in idempotency check via `server.shell` — `ufw status verbose` is parsed for current rules; the rule is added only if not present.

Edge case: the current box has IPv4 + IPv6 versions of the rule (UFW splits automatically). pyinfra task should detect both `[ 1] 22/tcp ... LIMIT IN ... Anywhere` AND `[ 2] 22/tcp (v6) ... LIMIT IN ... Anywhere (v6)` exist. If yes, no-op. Use `server.shell` with parsed output rather than `pyinfra.operations.iptables` (UFW manages iptables rules with its own naming, raw iptables ops would fight with UFW).

### _fail2ban.py

Drops a single file `/etc/fail2ban/jail.d/bubble.conf`:

```
[DEFAULT]
maxretry = {{ bans.maxretry }}
findtime = {{ bans.findtime_minutes }}m
bantime = {{ bans.bantime_hours }}h

[sshd]
enabled = true
mode = aggressive

[recidive]
enabled = true
maxretry = 3
findtime = 1d
bantime = 1w
```

`systemd.service('fail2ban', running=True, enabled=True, restarted=<file_changed>)` — fail2ban requires restart (not reload) to pick up jail.d changes.

### _unattended.py

Drops `/etc/apt/apt.conf.d/52unattended-upgrades-bubble`:

```
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "{{ auto_reboot_time }}";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
```

Verifies `unattended-upgrades.service` is enabled (it's enabled by default in Ubuntu 24.04 minimal).

### _swap.py

Three-step:

1. Create swapfile if missing: `server.shell` with `test -f /swapfile || (fallocate -l ${SIZE}G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile)`
2. Persist in `/etc/fstab`: `files.line(path='/etc/fstab', line='/swapfile none swap sw 0 0', present=True)`
3. Set swappiness in `/etc/sysctl.d/99-bubble.conf` via `files.line(path='/etc/sysctl.d/99-bubble.conf', line='vm.swappiness = 10', present=True)`. After change: `server.shell('sysctl -p /etc/sysctl.d/99-bubble.conf')`.

Idempotency: each step has a guard. Re-run on a box with `/swapfile` present + fstab line + sysctl line = zero changes.

### _ntp.py

```
apt install -y chrony
systemctl enable --now chrony
```

Idempotent via pyinfra's `apt.packages` + `systemd.service`.

---

## Test plan

### Unit tests (mock pyinfra host)

For each sub-module:
- `test_<module>_writes_expected_config()` — render the template with default tenant values, compare to a golden file
- `test_<module>_validates_inputs()` — pass invalid config (e.g. swap size_gb=0), expect raise

### Integration test (the dogfood validation)

`tests/integration/test_dogfood_hardening.sh`:

```bash
#!/bin/bash
set -euo pipefail

cd /Users/joris/claude-workspaces/rnd/projects/bubble-vps-platform

# Run the full hardening task once and capture pyinfra's "Changed" count
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/hardening/linux.py 2>&1 | tee /tmp/hardening-run1.log
CHANGES=$(grep "Grand total" /tmp/hardening-run1.log | awk '{print $5}')
NO_CHANGES=$(grep "Grand total" /tmp/hardening-run1.log | awk '{print $7}')

echo "Changed: $CHANGES, No-change: $NO_CHANGES"

if [ "$CHANGES" != "0" ]; then
    echo "❌ DOGFOOD FAILURE: pyinfra reported $CHANGES changes against the already-hardened joris-cx33."
    echo "   This means there's drift between the playbook and reality. Review the changes:"
    grep -E "(Changed|Started|Stopped|Wrote|Installed)" /tmp/hardening-run1.log
    exit 1
fi

echo "✅ DOGFOOD PASS: zero changes against joris-cx33."

# Run again to confirm 2nd-run is also zero (sanity check)
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/hardening/linux.py 2>&1 | tee /tmp/hardening-run2.log
CHANGES2=$(grep "Grand total" /tmp/hardening-run2.log | awk '{print $5}')

if [ "$CHANGES2" != "0" ]; then
    echo "❌ IDEMPOTENCY FAILURE: 2nd run reported $CHANGES2 changes."
    exit 1
fi

echo "✅ IDEMPOTENCY PASS: 2nd run is also zero changes."
```

Both runs must succeed for Step 2 to be considered complete.

### Negative test (drift detection)

After the dogfood passes, deliberately introduce drift to verify the playbook DETECTS it:

```bash
# Manually break ufw on the box
ssh hetzner 'sudo ufw disable'

# Re-run hardening
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/hardening/linux.py

# Expected: pyinfra reports 1 change (ufw re-enabled)
# Cleanup: zero changes after re-running once
```

Document this drift test in `docs/RUNBOOK.md` as "How to verify hardening playbook is healthy."

---

## Acceptance criteria for Step 2

Step 2 is DONE when:

1. ✅ All 6 sub-modules implemented + tested
2. ✅ Dogfood validation passes: `pyinfra ... hardening/linux.py` against joris-cx33 reports **zero changes** on first run
3. ✅ 2nd run also reports zero changes (idempotent)
4. ✅ Manual drift test shows pyinfra detects + corrects an introduced drift
5. ✅ Unit tests for templates pass (golden file compare)
6. ✅ `deploy.py` is updated to call `tasks.hardening.linux.apply()` for `linux_hosts`
7. ✅ Documentation updated:
   - `docs/RUNBOOK.md` mentions the drift test
   - `specs/SPEC-005-linux-hardening.md` checked off as Implemented

---

## Open questions

1. **NTP/chrony was NOT explicitly listed in the original 7-step hardening.** Do we add it as item 8 because it's table-stakes, or skip to match reality? Recommendation: **add it.** Ubuntu 24.04 ships with `systemd-timesyncd` enabled, which provides NTP — but chrony is more accurate and the standard for hardened production boxes. Adding it is a one-time small change that the playbook will report on the first dogfood run; subsequent runs are clean. **Option B** (defer): leave timesyncd alone, document in RUNBOOK that chrony is recommended but not enforced. Decision for Joris.

2. **Sysctl file naming** — `/etc/sysctl.d/99-bubble.conf` collides with future tenant-specific overrides. Recommendation: scope it as `99-bubble-platform.conf` (note the suffix). Late-numbered (99) so it overrides distro defaults but allows tenant additions in `100-tenant.conf`.

3. **Should the hardening task ever DOWNGRADE security?** E.g. if `cfg.sshd.permit_root_login: yes`, do we set it back? Recommendation: **yes**, the playbook is the source of truth. But ALWAYS print a clear warning in pyinfra output if a less-secure setting is being applied. This catches misconfigured tenant.yaml files.

4. **Are we OK with `apt update` running implicitly?** pyinfra `apt.packages` doesn't run `apt update` by default. We should run `apt.update(cache_time=3600)` once at the top of `linux.py` so package installs see fresh indexes, but cache for an hour to avoid hammering. Recommendation: yes.
