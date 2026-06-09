# SPEC-014 — Cloud security audit cron (Task B)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 1-6a done; Telegram channel `@ContentbubbleClawbot` shared with Morty
**Implements:** Task B of the post-Step-6a follow-up batch

---

## Purpose

Daily security audit running ON THE BOX (not on the Mac) that defends joris-cx33 against intrusion, drift, and credential-leakage. Posts findings to `@ContentbubbleClawbot` (per Joris's "share for now" decision in msg 1680). Ports the spirit of the Mac-side `security-daily-audit` cron, scoped to box-relevant checks only.

NOT a port of the Mac cron's full surface — that one audits Mac-specific things (~/Library, brew, agent watchdog across ALL agents). This is a leaner box-only audit.

---

## What it checks (per part)

### Part 1 — Auth & access (most critical)
- **fail2ban escalations:** `sudo fail2ban-client status sshd` — currently_banned count, recent ban events
- **sshd config drift:** `sudo sshd -T | grep -E "permitrootlogin|passwordauthentication|maxauthtries"` — must match SPEC-005 hardening (no, no, 3)
- **sshd recent failures:** `sudo last -F -50 | grep "still logged in\|reboot"` — anomaly check
- **Sudo entries:** `sudo cat /etc/sudoers.d/*` — list all NOPASSWD grants. Diff against expected baseline (claude-NOPASSWD-all + claude-telegram-watchdog).
- **/etc/passwd diff:** any unexpected user accounts since last audit.

### Part 2 — Secrets layer integrity
- **`/etc/age/key.txt` mode:** must be `400 root:root`. Anything else = critical.
- **`/etc/age/key.pub` exists:** hash compare against the version stored in `bubble-vps-data/tenants/bubble-internal/box-pubkey.txt` — drift means the box's age key was regenerated (security event).
- **`/etc/bubble/secrets.sops.env` mode:** must be `440 root:root`.
- **`/run/claude-agent/env` mode:** must be `400 claude:claude` (when service active). Tmpfs check: `findmnt /run` should show tmpfs.
- **No plaintext secrets on disk:** grep `/home /etc /root` for `sk-or-v1\|sk-ant-oat01-\|tskey-auth-\|^TELEGRAM_BOT_TOKEN=[0-9]` (excluding /etc/bubble/ and /run/) — should find ZERO matches outside the encrypted blob.

### Part 3 — Agent behavior
- **Service uptime:** how long has `claude-agent-morty.service` been running. >7d without restart = note for awareness.
- **Recent restarts:** `journalctl -u claude-agent-morty.service --since "24h ago" | grep -c "Started\|Stopped"` — anomaly if >10 in 24h (restart loop).
- **Telegram plugin liveness:** integrate with the Task D watchdog's check — bot.pid present + alive + bun process running.
- **Tailscale online:** `tailscale status --json` shows Self.Online == true.

### Part 4 — Package CVEs
- **Apt-listbugs / unattended-upgrades dry-run:** `apt list --upgradable 2>/dev/null` — list packages with security updates pending.
- **Specifically critical pkgs:** sshd, sudo, openssl, curl, bun (if available via apt), python3, sops, age. Flag if any have pending updates.
- **Last unattended-upgrades run:** `journalctl -u unattended-upgrades.service --since "24h ago" | tail -3` — verify it ran in the last 24h.

### Part 5 — Disk & memory health
- **Disk usage:** `df -h /` — alert if >85%
- **Inode usage:** `df -i /` — alert if >85%
- **Memory:** `free -h` — alert if swap usage >50%
- **Tmpfs `/run`:** alert if usage >70%

### Part 6 — Transcript leak scan (G.8 ported)
The Mac cron's G.8 step scans `~/.claude/projects/*/*.jsonl` for embedded credential prefixes (sk-ant-oat01-, sk-or-v1-, tskey-auth-, etc.). Same logic on the box for `/home/claude/.claude/projects/`.

### Part 7 — Claude Code version drift (G.9 ported)
- Installed: `claude --version`
- Latest available: `npm view @anthropic-ai/claude-code version`
- If installed != latest by more than 5 patches, note for awareness (we want eventual update but not urgent)
- If installed is OLDER than the version recorded at last successful service start (drift signal), restart needed

### Part 8 — Hetzner Cloud Firewall
- Verify the `bubble-default` firewall is still attached to the server: `hcloud server describe joris-cx33 -o json | jq .protection`
- (Skip if hcloud not installed on the box — runs from operator side as a separate check)

---

## Reporting

Single Telegram message to `@ContentbubbleClawbot` chat, formatted as:

```
🛡 Security audit — joris-cx33 — 2026-05-09 09:00 UTC
Score: 95/100  (was 95 yesterday)

✅ Auth & access (10/10): 0 banned, sshd clean, no new sudo grants
✅ Secrets layer (10/10): all modes correct, no plaintext leaks
✅ Agent (10/10): morty up 14h, telegram polling, tailscale online
⚠ CVEs (8/10): 3 packages with security updates pending (curl, openssl, openssh-client)
✅ Disk (10/10): / 12% used, swap 0%
✅ Transcripts (10/10): 0 credential prefix matches
⚠ Version (8/10): claude 2.1.131, latest 2.1.133 (2 patches behind, no action needed yet)
✅ Firewall (10/10): bubble-default attached

Action items: none
Next audit: tomorrow 09:00 UTC
```

Score is a coarse 100-point sum of the 8 parts. Below 80 = post in red. Findings persist to `/var/log/bubble-security/audit-<date>.log`.

---

## Schedule

Daily at 09:00 UTC (tunable). systemd timer (matches Tailscale watchdog pattern).

---

## Implementation

### Module: `/home/claude/scripts/security-audit.sh`

Bash, ~250 LOC. Breaks into 8 functions, one per part, each emits a `result_<n>` line. Final summary builds the score and Telegram message.

### systemd-timer + service: `/etc/systemd/system/bubble-security-audit.{timer,service}`

```ini
# bubble-security-audit.timer
[Unit]
Description=Bubble VPS security audit (daily 09:00 UTC)

[Timer]
OnCalendar=*-*-* 09:00:00 UTC
Persistent=true     # catch up if box was off at scheduled time
Unit=bubble-security-audit.service

[Install]
WantedBy=timers.target
```

```ini
# bubble-security-audit.service
[Unit]
Description=Bubble VPS security audit (one-shot)
After=network-online.target

[Service]
Type=oneshot
User=claude
ExecStart=/home/claude/scripts/security-audit.sh
StandardOutput=journal
StandardError=journal
```

### Sudoers drop-in: extends Task D's drop-in
`/etc/sudoers.d/claude-security-audit`:
```
claude ALL=(ALL) NOPASSWD: /usr/bin/fail2ban-client status sshd, /usr/sbin/sshd -T, /usr/bin/last -F -50, /bin/cat /etc/sudoers.d/*, /bin/cat /etc/passwd, /usr/bin/journalctl -u *
```

Tightly scoped — only the read commands the audit needs. NOT broad sudo.

### pyinfra task: `pyinfra/tasks/access/security_audit.py`

Same shape as the Tailscale and Telegram-watchdog tasks. Renders bash from template, drops systemd units, drops sudoers, daemon-reload, enable+start timer.

---

## SPEC-008 hard rule compliance

- The bash script reads `/run/claude-agent/env` to get the bot token (for posting alerts) — captures into shell var, never echos. `unset TOKEN` at end.
- The transcript-leak scan (Part 6) uses `grep -l` (list files) NOT `grep` (which would print matching lines containing the secrets). Critical distinction.
- The audit log file (`/var/log/bubble-security/audit-<date>.log`) is mode `0640 root:adm` — readable by sudo + adm group only, NOT by claude user (so a compromised agent can't read its own audit).
- The Telegram message includes COUNTS (e.g. "3 packages pending") and CATEGORIES (which packages), but NEVER credentials or sensitive paths.

---

## Test plan

### Static tests in `lib/test_security_audit.py` (new file)

1. `test_audit_script_no_plaintext_secrets` — render the bash template with bubble-internal cfg, grep for known credential prefixes, must find zero
2. `test_audit_script_uses_grep_l_for_transcript_scan` — assert the script uses `grep -l` (file names only) NOT bare `grep` for the transcript scan, so matched secret values aren't echoed
3. `test_audit_script_unsets_token_after_use` — same as Task D
4. `test_audit_log_file_mode_0640` — assert the script writes audit logs with `chmod 0640`
5. `test_audit_pyinfra_module_drops_sudoers` — static check the module installs the sudoers drop-in
6. `test_audit_systemd_units_render` — golden compare for .timer and .service

### Integration test (after deploy)

Manual on the box:
1. `sudo /home/claude/scripts/security-audit.sh` — runs to completion, posts to Telegram
2. Verify Telegram message arrives with all 8 parts checked
3. Verify `/var/log/bubble-security/audit-<date>.log` exists, mode 0640
4. Verify timer is active: `systemctl list-timers | grep bubble-security`
5. Damage test: `sudo chmod 644 /etc/age/key.txt` (deliberate drift), re-run audit, expect Part 2 to flag CRITICAL. Restore: `sudo chmod 400 /etc/age/key.txt`.

---

## Acceptance criteria

Task B done when:
1. ✅ Audit script + systemd timer + sudoers drop-in installed via pyinfra
2. ✅ Timer is active + enabled
3. ✅ Manual run produces a green report and posts to Telegram
4. ✅ Damage test: manual drift introduces a flag, audit catches it
5. ✅ 6 new static tests pass
6. ✅ All previous tests still pass
7. ✅ pyinfra deploy idempotent
8. ✅ deploy logs grep clean for credential prefixes

---

## Out of scope

- Multi-tenant rendering (the audit checks ONE tenant for now — multi-tenant is when client #1 lands)
- Persistent baseline diff for /etc/passwd, sudoers (would require a snapshot file; v1 just lists current state, operator eyeballs)
- Auto-remediation (we ALERT, we don't auto-fix; that's a future judgment call per finding)
- `bubble-sentinel` Stripe-fraud module from the Mac side (totally different concern)
