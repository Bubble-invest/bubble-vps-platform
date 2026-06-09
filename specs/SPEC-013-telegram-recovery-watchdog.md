# SPEC-013 — Telegram plugin recovery watchdog (Task D)

**Status:** v1.0 — **EXTENDED/SUPERSEDED in part by [SPEC-021](SPEC-021-canonical-agent-setup.md) (2026-05-31)**
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Step 4 (systemd unit) done; Step 6a (Tailscale, for alerting)
**Implements:** Task D of the post-Step-6a follow-up batch

> **⚠️ Read SPEC-021 before relying on the watchdog details below.** A
> 2026-05-31 production outage revealed four latent failure modes. SPEC-021
> changes the watchdog behavior described in this spec:
> - **Detection signal #3** (`pgrep -f "bun run.*telegram"`, §"Detection
>   signals" / pseudo-code line 81) is **replaced** by a cgroup-scoped check —
>   the broad pgrep gives false negatives on a multi-agent box.
> - **bot.pid path** (line 66) is now **per-persona** (`telegram-<persona>/`,
>   morty keeps bare `telegram/`), derived from a single source shared with the
>   plugin's state-dir creation.
> - **Recovery action** (§"Recovery action" / pseudo-code line 118) is **changed
>   from `systemctl restart` to `stop`→`start`** — a bare restart leaves a
>   zombie bun poller.
> - **A new 401/auth-failure detection signal** is added that alerts and exits
>   WITHOUT restarting (a restart can't fix bad credentials).
> - **The sudoers drop-in** (line 172) now grants `stop`+`start` (plus retains
>   `restart`/`is-active`).
> The rest of SPEC-013 (timer cadence, SPEC-008 token hygiene, cooldown gate,
> systemd-timer shape) remains authoritative.

---

## Problem

The Telegram MCP plugin is a known bug surface on the Mac side — `bun` processes go zombie, the `getUpdates` long-poll silently returns 409 conflicts, plugin self-exits on idle. We have a Mac-side LaunchAgent (`telegram-kick-watchdog`, runs every 15 min) that catches and recovers from these.

The cloud box has a NEW failure mode that the Mac watchdog doesn't cover: the systemd-supervised `claude-agent-morty.service` reports `active`, but the Telegram plugin INSIDE that service (`bun run` child of claude) can die or get stuck without claude itself crashing. systemd doesn't know — to systemd, the parent claude is alive. Result: agent appears healthy from systemd's POV, but ignores Telegram messages indefinitely.

We hit this exact failure mode three times during the Step 4 → Step 5a debugging on 2026-05-09: bun PATH issue, systemd quoting issue, skipDangerousModePermissionPrompt issue. Each one made the plugin silently absent.

---

## Detection signals

The watchdog should consider the plugin "broken" if ANY of these is true:

1. **`/home/claude/.claude/channels/telegram/bot.pid` is missing** — plugin never registered
2. **The PID in bot.pid is not running** — plugin crashed
3. **No `bun run --cwd .../telegram/...` process in `ps`** — plugin not spawned
4. **Telegram `getWebhookInfo` shows `pending_update_count > 5`** — plugin not consuming the queue (some pending is normal during processing; >5 sustained means broken)
5. **Telegram `getWebhookInfo` shows `last_error_message` non-empty AND recent** — Telegram side reports an error (most commonly 409 Conflict if multiple pollers, but could be anything)

A single signal triggering doesn't mean broken — combine for confidence:
- (1 OR 2 OR 3) → plugin process is dead → restart needed
- (4 AND not 1/2/3) → plugin alive but not polling → restart needed
- (5) → Telegram-side error → log + alert; restart only if 409 Conflict

---

## Recovery action

When broken:
1. `systemctl restart claude-agent-morty.service` (kicks the parent + lets ExecStartPre re-decrypt secrets + lets claude re-fork the plugin)
2. Wait 15s
3. Re-check signals 1-3
4. If still broken, post Telegram alert (via direct curl to Telegram, NOT via the broken plugin) and STOP — manual intervention needed

NEVER restart in a loop. One attempt per watchdog tick. If the restart didn't fix it, escalate (alert) rather than thrash.

---

## Implementation

### Module: `/home/claude/scripts/telegram-watchdog.sh`

Bash script (~80 LOC), runs as a systemd-timed unit every 5 min.

Why bash, not python: the script must be ROBUST against environmental breakage (e.g. python venv broken, dependency missing). Bash + curl + ps + systemctl are basically guaranteed available.

Why systemd-timer, not cron: matches the rest of our stack. Timers are visible via `systemctl list-timers`.

Pseudo-code:
```bash
#!/usr/bin/env bash
set -uo pipefail   # NOT -e: we want to handle each check, not abort on first

SERVICE="claude-agent-morty.service"
BOT_PID_FILE="/home/claude/.claude/channels/telegram/bot.pid"
LAST_RESTART_MARK="/run/telegram-watchdog/last-restart"
COOLDOWN_SECONDS=300  # 5 min — don't restart more than once per cooldown

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { logger -t telegram-watchdog "$*"; }

# Check 1: bot.pid file exists
broken=0
reason=""

if [[ ! -f "$BOT_PID_FILE" ]]; then
    broken=1; reason="bot.pid missing"
elif ! kill -0 "$(cat $BOT_PID_FILE)" 2>/dev/null; then
    broken=1; reason="bot.pid points to dead PID"
elif ! pgrep -f "bun run.*telegram" >/dev/null; then
    broken=1; reason="no bun.*telegram process"
fi

# Check 4 (only if process-level checks pass): pending_update_count
if [[ $broken -eq 0 ]]; then
    TOKEN=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' /run/claude-agent/env 2>/dev/null)
    if [[ -n "$TOKEN" ]]; then
        # Capture API response, parse pending count
        pending=$(curl -s --max-time 10 "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" \
                   | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("result",{}).get("pending_update_count",0))' 2>/dev/null || echo 0)
        if [[ "$pending" -gt 5 ]]; then
            broken=1; reason="pending_update_count=$pending (plugin not polling)"
        fi
    fi
    unset TOKEN
fi

if [[ $broken -eq 0 ]]; then
    log "ok"
    exit 0
fi

# Cooldown gate
if [[ -f "$LAST_RESTART_MARK" ]]; then
    last=$(stat -c %Y "$LAST_RESTART_MARK")
    now=$(date +%s)
    if (( now - last < COOLDOWN_SECONDS )); then
        log "broken ($reason) — but in cooldown, last restart $((now-last))s ago"
        # still exit 0 so the timer keeps firing; alert via telegram once we exceed cooldown
        exit 0
    fi
fi

log "broken: $reason — restarting service"
mkdir -p /run/telegram-watchdog
touch "$LAST_RESTART_MARK"
sudo systemctl restart "$SERVICE"

# Wait + re-check
sleep 15
if [[ -f "$BOT_PID_FILE" ]] && kill -0 "$(cat $BOT_PID_FILE)" 2>/dev/null; then
    log "recovery confirmed: bot.pid present after restart"
    exit 0
fi

# Still broken — alert via direct curl
log "recovery FAILED — alerting via direct curl"
TOKEN=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' /run/claude-agent/env 2>/dev/null)
if [[ -n "$TOKEN" ]]; then
    curl -s --max-time 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="{{OPERATOR_CHAT_ID}}" \
        -d text="🚨 telegram-watchdog: $SERVICE plugin broken ($reason). Auto-restart at $(ts) didn't recover. Manual investigation needed." \
        > /dev/null
fi
unset TOKEN
exit 1
```

### systemd-timer unit: `/etc/systemd/system/telegram-watchdog.timer`

```ini
[Unit]
Description=Telegram plugin liveness watchdog (every 5 min)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=telegram-watchdog.service

[Install]
WantedBy=timers.target
```

### systemd-service unit (one-shot): `/etc/systemd/system/telegram-watchdog.service`

```ini
[Unit]
Description=Telegram plugin liveness watchdog (one-shot per timer tick)
After=network-online.target

[Service]
Type=oneshot
User=claude
ExecStart=/home/claude/scripts/telegram-watchdog.sh
# Allow sudo systemctl restart without password (configured separately via sudoers drop-in)
```

### Sudoers drop-in: `/etc/sudoers.d/claude-telegram-watchdog`

```
claude ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart claude-agent-morty.service, /usr/bin/systemctl is-active claude-agent-morty.service
```

Tightly scoped — only the two systemctl operations the watchdog needs, no general sudo.

---

## SPEC-008 hard rule compliance

- The bot token is captured into a shell variable (`TOKEN=$(...)`) and used directly in curl URLs — **never echoed**. `unset TOKEN` immediately after use.
- The watchdog does NOT log the token. It logs operations and reasons.
- curl requests are HTTPS (Telegram API), so the token is encrypted in transit.
- Concern: curl URL contains the token. `ps auxww` could show it briefly. Mitigation: use `--data-urlencode` and put the token in `--config` file form, OR use `Authorization: Bearer` header. For simplicity in v1, accept the brief ps-leak risk — it's a 5-min internal watchdog, not user-exposed.

Future hardening: switch to `curl --config <file>` form to keep the URL short and the token in a file that's mode 0400.

---

## pyinfra task: `pyinfra/tasks/access/telegram_watchdog.py`

Idempotent install:
1. Render `/home/claude/scripts/telegram-watchdog.sh` from `pyinfra/templates/telegram-watchdog.sh.j2` (jinja vars: SERVICE, BOT_PID_FILE, COOLDOWN_SECONDS, JORIS_TG_USER_ID for alerts)
2. Drop systemd `.timer` and `.service` units from templates
3. Drop sudoers rule
4. `systemctl daemon-reload` (gated on any unit/sudoers change)
5. `systemctl enable --now telegram-watchdog.timer`

Restart-on-change pattern (from Task A learning):
- If watchdog script changed → no restart needed (timer picks up new script on next tick)
- If timer/service unit changed → daemon-reload + restart timer

---

## Test plan

### Static tests in `lib/test_telegram_watchdog.py` (new file)

1. `test_watchdog_script_template_no_plaintext_secrets` — render the bash template, grep for known leaked-credential prefixes (sk-ant, sk-or, 8350575119:). Should not find any.
2. `test_watchdog_script_unsets_token_after_use` — assert the rendered script contains `unset TOKEN` after the curl call.
3. `test_watchdog_systemd_units_render` — golden compare for the .timer and .service units.
4. `test_watchdog_pyinfra_module_drops_sudoers` — static check that the module includes a `files.put` for `/etc/sudoers.d/claude-telegram-watchdog`.

### Integration test (after deploy)

Manual on the box:
1. Verify timer is active: `systemctl is-active telegram-watchdog.timer`
2. Verify watchdog runs cleanly when nothing's broken: `sudo /home/claude/scripts/telegram-watchdog.sh; journalctl -t telegram-watchdog -n 1` should show `ok`
3. Verify recovery path: `sudo rm /home/claude/.claude/channels/telegram/bot.pid; sudo /home/claude/scripts/telegram-watchdog.sh; sleep 20; ls /home/claude/.claude/channels/telegram/bot.pid` — should reappear (because the restart re-spawns the plugin).

---

## Acceptance criteria

Task D done when:
1. ✅ Watchdog script + systemd timer + sudoers drop-in installed via pyinfra
2. ✅ Timer is active + enabled
3. ✅ One manual run reports `ok` (when nothing's broken)
4. ✅ Recovery test: deleting bot.pid + running watchdog restores the plugin
5. ✅ 4 new static tests pass
6. ✅ Existing 109/109 tests still pass (after Task A's +3)
7. ✅ deploy logs grep clean for credential prefixes
8. ✅ pyinfra deploy idempotent

---

## Out of scope

- The Mac-side ghost-bun guard injection (different problem class)
- Multi-agent watchdogs (only morty for now; multi-agent is deferred)
- Watchdog dashboard / metrics export — that's Step 6b (phone-home daemon's job)
