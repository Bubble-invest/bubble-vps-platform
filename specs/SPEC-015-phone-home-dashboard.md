# SPEC-015 — Phone-home daemon + central dashboard skeleton (Task C)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 1-6a done; Tailscale up (for Tailscale-only dashboard exposure)
**Implements:** Task C of the post-Step-6a follow-up batch + first half of Step 6b in the master plan

---

## Purpose

Two co-deployed pieces:

1. **Phone-home daemon (per-tenant box):** small periodic process that POSTs telemetry to a central endpoint. NEVER sends data content — only metadata (service-up, disk %, last-Telegram-msg-ts, agent restart count, claude version, etc.). Proves the per-tenant heartbeat pattern that future client boxes will reuse.

2. **Central dashboard (lives on {{VPS_HOST}} for now):** receives phone-home POSTs, stores them in a tiny SQLite, exposes a single web view at `http://:3848/` (Tailscale-only — no public internet exposure). For now: just bubble-internal tenant. Multi-tenant rendering when client #1 lands.

Together: when you open the dashboard URL from any tailnet device, you see "{{VPS_HOST}}: green, last heartbeat 30s ago, claude 2.1.131, morty service up 14h, 0 telegram errors today". When client #1 lands, that becomes a row above ours.

---

## Telemetry contract (what the daemon sends)

POST `http://<dashboard>/heartbeat` every 5 min, JSON body:

```json
{
  "schema_version": 1,
  "tenant_name": "bubble-internal",
  "ts_utc": "2026-05-09T11:30:00Z",
  "host": {
    "hostname": "{{VPS_HOST}}",
    "uptime_seconds": 51234,
    "disk_pct_used": 12,
    "memory_pct_used": 23,
    "swap_pct_used": 0,
    "load_avg_1m": 0.12
  },
  "agent": {
    "service": "claude-agent-morty.service",
    "is_active": true,
    "is_enabled": true,
    "uptime_seconds": 5421,
    "restarts_24h": 0
  },
  "telegram": {
    "bot_pid_present": true,
    "bot_pid_alive": true,
    "pending_update_count": 0,
    "last_error_message": null
  },
  "tailscale": {
    "online": true,
    "self_ip": "{{INTERNAL_IP}}"
  },
  "claude_code": {
    "version_installed": "2.1.131",
    "version_latest_npm_cached": "2.1.133"
  }
}
```

**Critical security property:** NO data content (no message contents, no agent decisions, no file paths from the agent's work, no secrets, no tokens). Only operational counters + state booleans. A compromised dashboard would leak metadata, not data.

Authentication: each tenant has a shared `PHONEHOME_TOKEN` in their SOPS file. POSTs include `Authorization: Bearer <token>`. Dashboard verifies. For v1, single-tenant: a fixed token both sides know. Multi-tenant later: per-tenant tokens with rotation.

---

## Daemon implementation

### Module: `/home/claude/scripts/phone-home.sh`

Bash, ~120 LOC. Runs from systemd timer every 5 min. Captures all the data points above, builds JSON, POSTs.

For data points needing root (sshd state, journal counts), uses NOPASSWD sudoers entries (extends Task D's drop-in).

### systemd: `/etc/systemd/system/phone-home.{timer,service}`

```ini
# phone-home.timer
[Unit]
Description=Bubble VPS phone-home telemetry (every 5 min)

[Timer]
OnBootSec=30s
OnUnitActiveSec=5min
Unit=phone-home.service

[Install]
WantedBy=timers.target
```

```ini
# phone-home.service
[Unit]
Description=Bubble VPS phone-home telemetry (one-shot)
After=network-online.target tailscaled.service

[Service]
Type=oneshot
User=claude
ExecStart=/home/claude/scripts/phone-home.sh
StandardOutput=journal
StandardError=journal
```

---

## Dashboard implementation

### Lives at: `/home/claude/dashboard/` on {{VPS_HOST}}

For v1, smallest possible thing that works:
- **Backend:** single Python file `app.py` using stdlib `http.server` (no Flask, no extra deps)
- **Storage:** SQLite at `/var/lib/bubble-dashboard/heartbeats.db`, schema:
  ```sql
  CREATE TABLE heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_name TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL  -- the full posted JSON
  );
  CREATE INDEX idx_tenant_ts ON heartbeats(tenant_name, ts_utc DESC);
  ```
- **Endpoints:**
  - `POST /heartbeat` — auth bearer, validate JSON shape, insert row, return 200
  - `GET /` — render single HTML page showing latest heartbeat per tenant (table view)
  - `GET /tenant/<name>` — show last 24h of heartbeats for that tenant (timeline)
  - `GET /healthz` — return 200 ok (for monitoring)

### systemd: `/etc/systemd/system/bubble-dashboard.{service}` (no timer — long-running)

```ini
[Unit]
Description=Bubble VPS central dashboard (Tailscale-only, port 3848)
After=network-online.target

[Service]
Type=simple
User=claude
ExecStart=/usr/bin/python3 /home/claude/dashboard/app.py
Restart=on-failure
RestartSec=10
WorkingDirectory=/home/claude/dashboard
Environment=DB_PATH=/var/lib/bubble-dashboard/heartbeats.db
Environment=BIND_ADDR={{INTERNAL_IP}}  # Tailscale IP — bind only on tailnet, NOT 0.0.0.0
Environment=BIND_PORT=3848

[Install]
WantedBy=multi-user.target
```

**Critical:** `BIND_ADDR=<tailscale-ip>` — dashboard listens ONLY on the tailnet interface, not on 0.0.0.0. Cannot be reached via public internet even if UFW were misconfigured. Defense in depth.

**The Tailscale IP changes** — actually it's stable per-device once registered, but we should NOT hardcode `{{INTERNAL_IP}}` in the systemd template. Better: derive at start time via `tailscale ip -4` and pass as env var.

Updated `ExecStart`:
```
ExecStartPre=+/bin/sh -c 'echo "BIND_ADDR=$(tailscale ip -4 | head -1)" > /run/bubble-dashboard.env'
EnvironmentFile=-/run/bubble-dashboard.env
ExecStart=/usr/bin/python3 /home/claude/dashboard/app.py
```

---

## Auth token

`PHONEHOME_TOKEN` — generated once on the operator side as a 32-char random string, pasted via `operator-set-secret.sh`, encrypted into SOPS, exposed to BOTH the daemon AND the dashboard via the standard env-file path.

Daemon: reads from `/run/claude-agent/env`.
Dashboard: also reads from `/run/claude-agent/env` (since dashboard runs as `claude` user too, has read access).

Multi-tenant later: each tenant has its own token; dashboard maintains a token→tenant_name map.

---

## SPEC-008 hard rule compliance

- `PHONEHOME_TOKEN` is captured into shell var, never echoed.
- POST body is JSON-encoded (no shell interpolation of values).
- Dashboard logs POST payload size + tenant_name + timestamp, NOT the payload itself.
- SQLite DB file mode 0640 root:claude (claude can read for the dashboard process).

---

## pyinfra task: `pyinfra/tasks/access/phone_home.py`

Per-tenant install. Same shape as Tailscale + watchdog tasks: render bash from template, drop systemd timer + service, drop sudoers extensions, daemon-reload, enable+start.

---

## pyinfra task: `pyinfra/tasks/monitoring/dashboard.py`

CENTRAL — runs once per box that hosts the dashboard. v1: only on `bubble-internal` (we are the host). Multi-tenant later: a flag in tenant.yaml says "this tenant hosts the dashboard for tenants X, Y, Z."

Installs Python app, SQLite db dir, systemd service. Idempotent.

---

## Test plan

### Static tests in `lib/test_phone_home.py` and `lib/test_dashboard.py`

For phone-home:
1. `test_phone_home_no_data_content_in_payload` — render an example payload (mock the data-collection functions), assert NO field contains agent message text, no secrets, no token values
2. `test_phone_home_unsets_token_after_use` — same as Task D
3. `test_phone_home_systemd_units_render` — golden compare

For dashboard:
1. `test_dashboard_binds_to_tailscale_ip_only` — assert the systemd unit derives BIND_ADDR from `tailscale ip -4`, NOT hardcoded 0.0.0.0
2. `test_dashboard_validates_bearer_token` — call the POST endpoint without auth, expect 401
3. `test_dashboard_db_schema` — assert the schema matches the spec
4. `test_dashboard_html_renders` — call `/`, assert basic HTML structure

### Integration test (after deploy)

Manual on the box:
1. `curl -H "Authorization: Bearer $TOKEN" -X POST http://{{INTERNAL_IP}}:3848/heartbeat -d '{"schema_version": 1, ...}'` → 200
2. Open `http://:3848/` from operator's Mac → see {{VPS_HOST}}'s row
3. `curl http://178.105.77.178:3848/` from outside the tailnet → connection refused (proves dashboard NOT exposed to public internet)
4. `systemctl status phone-home.timer` → active, last fired <5 min ago
5. After 10 min: 2-3 heartbeat rows in the SQLite

---

## Acceptance criteria

Task C done when:
1. ✅ Phone-home daemon installed + timer firing
2. ✅ Dashboard service installed, listening on tailnet IP only
3. ✅ Dashboard receives + stores heartbeats
4. ✅ Dashboard `/` renders a usable single-tenant view
5. ✅ Dashboard NOT reachable from public internet (verified via curl from outside)
6. ✅ Dashboard authenticates POSTs with bearer token
7. ✅ All static tests pass
8. ✅ All previous tests still pass
9. ✅ pyinfra deploy idempotent
10. ✅ deploy logs grep clean for credential prefixes

---

## Out of scope (deferred to future phases)

- Multi-tenant dashboard rendering (need >1 tenant first)
- Per-tenant token rotation (single token in v1)
- Historical metrics graphs (just shows latest + 24h timeline in v1)
- Alerts / paging from dashboard (v1 = passive view; alerting is separate)
- TLS for dashboard (Tailscale already encrypts the network path; HTTPS-on-tailnet is overkill)
- Dashboard ↔ Telegram (no agent talks to dashboard via Telegram in v1)

---

## Why this is the right shape

The dashboard is the missing piece for litmus criterion #3 ("All tenant health visible at a glance"). We're building it now while we have one tenant — the plumbing is the same as for many. When client #1 lands, the only delta is "add their tenant_name to the data repo, deploy phone-home there, dashboard automatically picks them up via the heartbeat row".

This also unblocks Phase 5d items (e.g. "claude version drift cron") — instead of building independent crons that each phone home, we have ONE collector that stores everything, and the dashboard surfaces specific concerns via different views.
