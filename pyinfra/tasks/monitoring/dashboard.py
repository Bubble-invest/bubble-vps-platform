"""Install the central Bubble VPS dashboard (SPEC-015, Task C).

CONDITIONALLY runs only on the dashboard-host tenant. v1: hardcoded to
`bubble-internal` — we are Tenant #1 + the dashboard host. Multi-tenant
later: tenant.yaml gains an `access.hosts_dashboard: true` flag and we
gate on that instead.

What this does, idempotently:
    1. Ensures /home/claude/dashboard/ exists (mode 0755 owner claude:claude).
       The app.py file goes here.
    2. Ensures /var/lib/bubble-dashboard/ exists (mode 0750 owner claude:claude).
       The SQLite DB lives here. Mode 0750 = claude can read/write,
       claude group can read, others have no access.
    3. Renders /home/claude/dashboard/app.py from a jinja2 template
       (mode 0755 owner claude:claude). Pure stdlib HTTP server +
       SQLite storage; ~400 LOC.
    4. Drops the systemd .service unit at /etc/systemd/system/bubble-dashboard.service
       (mode 0644 owner root:root). NOT a timer — this is a long-running
       Type=simple process. ExecStartPre derives BIND_ADDR from
       `tailscale ip -4` so the dashboard binds ONLY to the tailnet
       interface (defense-in-depth vs UFW misconfig).
    5. systemctl daemon-reload, gated on the unit changing on disk.
    6. systemctl enable --now bubble-dashboard.service.
    7. Restart the service if app.py changed (so a code update goes live
       without manual operator intervention).

DASHBOARD HOST GATING:
    v1 hardcoded check: cfg.tenant_name == "bubble-internal" → install.
    Anything else → skip. This is intentionally a one-line check so when
    tenant #2 lands the diff is small (replace with cfg.access.hosts_dashboard
    once that schema field is added).

NO sudoers needed — the dashboard process reads (DB_PATH, ENV_FILE) and
binds an unprivileged port (3848). The ExecStartPre that writes
/run/bubble-dashboard.env runs as root via the systemd `+` prefix.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import get_tenant_config


_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_APP_TEMPLATE = _TEMPLATES_DIR / "dashboard-app.py.j2"
_SERVICE_TEMPLATE = _TEMPLATES_DIR / "bubble-dashboard.service.j2"

_APP_DIR = "/home/claude/dashboard"
_APP_PATH = "/home/claude/dashboard/app.py"
_DB_DIR = "/var/lib/bubble-dashboard"
_SERVICE_PATH = "/etc/systemd/system/bubble-dashboard.service"

# v1 hardcoded host gate. Multi-tenant later: replace with a tenant.yaml
# schema flag (`access.hosts_dashboard: true`) once a 2nd tenant exists.
_DASHBOARD_HOST_TENANT = "bubble-internal"


def apply() -> None:
    """Install the dashboard app + service, only on the dashboard-host tenant."""
    cfg = get_tenant_config(host)

    # ─── Host gate ─────────────────────────────────────────────────────────
    if cfg.tenant_name != _DASHBOARD_HOST_TENANT:
        # Not the dashboard host — skip cleanly. Other tenants still run
        # phone-home (their daemon will POST here).
        return

    s = cfg.secrets
    if s is None or not s.enabled:
        # The dashboard reads PHONEHOME_TOKEN from the same env file as
        # the agent. Without the secrets layer there's no token source;
        # skip rather than ship a dashboard that can't authenticate.
        return
    decrypted_runtime_path = s.decrypted_runtime_path

    # ─── 1. Ensure /home/claude/dashboard/ exists ──────────────────────────
    # CLAUDE-OWNED target (/home/claude/dashboard) — the deploy connects AS
    # claude but pyinfra still needs explicit escalation to set ownership/mode
    # reliably. `_sudo=True, _sudo_user="claude"` so the dir ends up owned by
    # claude:claude (NOT root). Same convention as cloud_wiki_sync.py.
    files.directory(
        name=f"monitoring/dashboard: ensure {_APP_DIR} exists",
        path=_APP_DIR,
        present=True,
        mode="0755",
        user="claude",
        group="claude",
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 2. Ensure /var/lib/bubble-dashboard/ exists ───────────────────────
    # 0750 owner claude:claude — claude has full r/w (the dashboard process
    # writes the DB), claude group can read, others have no access.
    # ROOT escalation (NOT _sudo_user=claude): /var/lib/ is root-owned, so
    # claude cannot mkdir a subdir there — `sudo -u claude mkdir` would hit
    # Permission denied. Run as ROOT (`_sudo=True` ALONE); pyinfra's
    # user="claude"/group="claude" kwargs then chown the created dir to
    # claude:claude (root can chown). This is the one /var/lib path — it is
    # NOT under /home/claude, so it escalates to root, not claude.
    files.directory(
        name=f"monitoring/dashboard: ensure {_DB_DIR} exists",
        path=_DB_DIR,
        present=True,
        mode="0750",
        user="claude",
        group="claude",
        _sudo=True,
    )

    # ─── 3. Render the dashboard app ───────────────────────────────────────
    app_op = files.template(
        name=f"monitoring/dashboard: render {_APP_PATH}",
        src=str(_APP_TEMPLATE),
        dest=_APP_PATH,
        mode="0755",
        user="claude",
        group="claude",
        # Template variables (jinja2):
        tenant_name=cfg.tenant_name,
        decrypted_runtime_path=decrypted_runtime_path,
        # CLAUDE-OWNED target (/home/claude/dashboard/app.py) → escalate to
        # claude so the file ends up owned by claude:claude. `_sudo=True,
        # _sudo_user="claude"`.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 4. Drop the systemd unit ──────────────────────────────────────────
    service_op = files.template(
        name=f"monitoring/dashboard: drop {_SERVICE_PATH}",
        src=str(_SERVICE_TEMPLATE),
        dest=_SERVICE_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT-owned target (/etc/systemd/system/...) → `_sudo=True` ALONE (no
        # _sudo_user; the deploy connects AS claude, which cannot write /etc).
        _sudo=True,
    )

    # ─── 5. systemctl daemon-reload (gated) ────────────────────────────────
    # Only the unit file change triggers reload — app.py is read by python
    # at process start, not by systemd at unit load.
    server.shell(
        name="monitoring/dashboard: systemctl daemon-reload (only if unit changed)",
        commands=["systemctl daemon-reload"],
        _if=service_op.did_change,
        # systemctl daemon-reload on system units is root-only → `_sudo=True`.
        _sudo=True,
    )

    # ─── 6. Enable + start the service ─────────────────────────────────────
    # systemctl enable/start on a system service is root-only → `_sudo=True`.
    systemd.service(
        name="monitoring/dashboard: enable + start bubble-dashboard.service",
        service="bubble-dashboard.service",
        enabled=True,
        running=True,
        _sudo=True,
    )

    # ─── 7. Restart on app.py OR unit change ───────────────────────────────
    # If app.py changed we need the python process to re-exec to pick it up.
    # If the unit changed (e.g. ENV vars), restart picks up the new env.
    # Lambda-wrap the OR — see telegram_watchdog.py for the gotcha.
    server.shell(
        name="monitoring/dashboard: restart service (only if app or unit changed)",
        commands=["systemctl restart bubble-dashboard.service"],
        _if=lambda: app_op.did_change() or service_op.did_change(),
        # systemctl restart on a system service is root-only → `_sudo=True`.
        _sudo=True,
    )
