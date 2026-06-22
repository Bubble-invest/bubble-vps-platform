"""Install the per-tenant phone-home telemetry daemon (SPEC-015, Task C).

What this does, idempotently:
    1. Ensures /home/claude/scripts/ exists (mode 0755 owner claude:claude).
    2. Renders /home/claude/scripts/phone-home.sh from a jinja2 template
       (mode 0755 owner claude:claude). The bash script collects operational
       metadata (host uptime/disk/mem, agent service state, telegram plugin
       liveness, tailscale IP, claude code version) and POSTs a JSON
       payload to the central dashboard via curl with a Bearer token in
       the Authorization header.
    3. Drops the systemd .timer + .service units at
       /etc/systemd/system/phone-home.{timer,service} (mode 0644 owner
       root:root). Timer fires every 5 min; service is oneshot under User=claude.
    4. systemctl daemon-reload, gated on either the timer or service unit
       actually changing on disk (lambda-wrapped did_change check — same
       gotcha as telegram_watchdog.py + security_audit.py).
    5. systemctl enable --now phone-home.timer.

NO sudoers needed: every read the daemon performs (free, df, /proc, journalctl,
systemctl is-active, /home/claude/.claude/channels/telegram/bot.pid,
{decrypted_runtime_path}) is claude-readable as-is.

Sudo escalation (deploy connects AS claude, NOT root): each op escalates
explicitly per the implementation-log.md rule. ROOT targets (/etc/systemd/system
units) + root-only systemctl commands → `_sudo=True` ALONE. CLAUDE targets
(/home/claude/scripts/{,phone-home.sh}) → `_sudo=True, _sudo_user="claude"`.
There is NO global pyinfra-as-root; missing escalation → Permission denied →
`No hosts remaining!` at deploy time.

SPEC-008 hard rule (no plaintext credential to stdout/stderr):
    The bash script reads PHONEHOME_TOKEN from {decrypted_runtime_path} into
    a shell variable and uses it ONCE in an `Authorization: Bearer ${TOKEN}`
    header on a POST to the dashboard, then immediately `unset TOKEN`. The
    token NEVER appears in the URL (would show in `ps auxww`) and is never
    echoed. See test_phone_home.py for static enforcement.

DASHBOARD URL:
    Defaults to the dashboard host's tailnet IP on port 3848. Override per
    deployment with BUBBLE_DASHBOARD_URL (the tailnet IP of {{VPS_HOST}}, where
    the dashboard lives — see tasks/monitoring/dashboard.py).
    Multi-tenant later: this becomes a tenant.yaml field
    (access.phone_home.dashboard_url) once we have a 2nd tenant. The schema
    field already exists as `dashboard_url_secret_ref` for the SOPS-stored
    URL case; keeping v1 hardcoded avoids burning a SOPS key on a value
    that's literally a tailnet IP we control.
"""

from __future__ import annotations

import os
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import get_tenant_config


_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_SCRIPT_TEMPLATE = _TEMPLATES_DIR / "phone-home.sh.j2"
_TIMER_TEMPLATE = _TEMPLATES_DIR / "phone-home.timer.j2"
_SERVICE_TEMPLATE = _TEMPLATES_DIR / "phone-home.service.j2"

_SCRIPT_DIR = "/home/claude/scripts"
_SCRIPT_PATH = "/home/claude/scripts/phone-home.sh"
_TIMER_PATH = "/etc/systemd/system/phone-home.timer"
_SERVICE_PATH = "/etc/systemd/system/phone-home.service"

_BOT_PID_FILE = "/home/claude/.claude/channels/telegram/bot.pid"

# v1: dashboard lives on {{VPS_HOST}}; tailnet IP is stable per-device once
# registered. Override per deployment with BUBBLE_DASHBOARD_URL; the default
# uses a placeholder tailnet IP so the OSS repo ships no real infra address.
# Multi-tenant later: derive from a tenant.yaml field once a 2nd tenant exists.
_DASHBOARD_URL_V1 = os.environ.get(
    "BUBBLE_DASHBOARD_URL", "http://100.64.0.1:3848/heartbeat"
)


def apply() -> None:
    """Install the phone-home timer + service + script."""
    cfg = get_tenant_config(host)

    # The daemon needs the agent's tenant context: persona name (→ service
    # name to query systemctl is-active on), the decrypted runtime env path
    # (→ where to read PHONEHOME_TOKEN from), and the tenant name (→ POST
    # payload identifier so the dashboard can group rows by tenant).
    persona_name = cfg.agent.persona.name
    service_name = f"claude-agent-{persona_name}.service"

    s = cfg.secrets
    if s is None or not s.enabled:
        # Without the secrets layer there's no decrypted env file to read
        # PHONEHOME_TOKEN from. Skip cleanly — same opt-out shape as the
        # watchdog and audit.
        return
    decrypted_runtime_path = s.decrypted_runtime_path

    # Phone-home is opt-in per tenant via access.phone_home.enabled. Skip
    # cleanly when disabled rather than installing a no-op timer.
    if not cfg.access.phone_home.enabled:
        return

    # ─── 1. Ensure /home/claude/scripts/ exists ────────────────────────────
    files.directory(
        name="access/phone_home: ensure /home/claude/scripts/ exists",
        path=_SCRIPT_DIR,
        present=True,
        mode="0755",
        user="claude",
        group="claude",
        # CLAUDE target: deploy connects AS claude; escalate as claude so the
        # dir is owned claude:claude (not root). See implementation-log.md.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 2. Render the daemon bash script ──────────────────────────────────
    files.template(
        name=f"access/phone_home: render {_SCRIPT_PATH}",
        src=str(_SCRIPT_TEMPLATE),
        dest=_SCRIPT_PATH,
        mode="0755",
        user="claude",
        group="claude",
        # Template variables (jinja2):
        service_name=service_name,
        decrypted_runtime_path=decrypted_runtime_path,
        tenant_name=cfg.tenant_name,
        dashboard_url=_DASHBOARD_URL_V1,
        bot_pid_file=_BOT_PID_FILE,
        # CLAUDE target: script lives under /home/claude, owned claude:claude.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 3. Drop systemd timer + service units ─────────────────────────────
    timer_op = files.template(
        name=f"access/phone_home: drop {_TIMER_PATH}",
        src=str(_TIMER_TEMPLATE),
        dest=_TIMER_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT target: /etc/systemd/system — root-only write (NO _sudo_user).
        _sudo=True,
    )
    service_op = files.template(
        name=f"access/phone_home: drop {_SERVICE_PATH}",
        src=str(_SERVICE_TEMPLATE),
        dest=_SERVICE_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT target: /etc/systemd/system — root-only write (NO _sudo_user).
        _sudo=True,
    )

    # ─── 4. systemctl daemon-reload (gated) ────────────────────────────────
    # Same lambda-wrapping gotcha as telegram_watchdog: bound did_change
    # methods are always truthy, so we wrap in a lambda evaluated at exec
    # time.
    server.shell(
        name="access/phone_home: systemctl daemon-reload (only if units changed)",
        commands=["systemctl daemon-reload"],
        _if=lambda: timer_op.did_change() or service_op.did_change(),
        # ROOT command: systemctl is root-only (NO _sudo_user).
        _sudo=True,
    )

    # ─── 5. Enable + start the timer ───────────────────────────────────────
    systemd.service(
        name="access/phone_home: enable + start phone-home.timer",
        service="phone-home.timer",
        enabled=True,
        running=True,
        # ROOT command: systemctl enable/start is root-only (NO _sudo_user).
        _sudo=True,
    )
    server.shell(
        name="access/phone_home: restart timer (only if timer unit changed)",
        commands=["systemctl restart phone-home.timer"],
        _if=timer_op.did_change,
        # ROOT command: systemctl is root-only (NO _sudo_user).
        _sudo=True,
    )
