"""Install wiki-compile failure alerting (2026-06-05, Rick).

The nightly cloud-wiki-compile@compile.service (transcript mining -> shared
wiki, 22:00 UTC) only notified on SUCCESS. A failed or never-fired run was
silent. This task installs two complementary guards, idempotently:

  1. A GENERIC reusable OnFailure handler -- cron-failure-alert@.service +
     /home/claude/scripts/cron-failure-alert.sh. Any one-shot cron can wire
     OnFailure=cron-failure-alert@%n.service to get a Telegram alert with the
     failed unit + last journal lines. Catches NON-ZERO EXITS.
  2. A daily freshness watchdog -- wiki-compile-freshness.{service,timer} +
     /home/claude/scripts/wiki-compile-freshness.sh (09:00 UTC). Alerts if no
     SUCCESSFUL compile happened in ~26h. Catches the case OnFailure can't see:
     a compile that NEVER FIRED (timer disabled, box asleep at 22:00, masked).

It also wires the OnFailure drop-in onto the compile service via a drop-in at
/etc/systemd/system/cloud-wiki-compile@.service.d/onfailure.conf.

Owner-model mirrors cloud_wiki_sync.py: scripts are claude:claude under
/home/claude/scripts/ (_sudo=True, _sudo_user="claude"); systemd units are
root:root under /etc/systemd/system/ (_sudo=True alone). Opt-out shape identical
to the watchdog/audit: no secrets or no contact -> skip cleanly.

NOTE: the cloud-wiki-COMPILE units themselves are NOT yet managed by pyinfra
(deployed ad-hoc). This task installs the ALERTS and the OnFailure drop-in; if
the compile units are absent the freshness watchdog still works and the drop-in
is inert until the compile service exists.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import get_tenant_config

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_ALERT_SH_TPL = _TEMPLATES_DIR / "cron-failure-alert.sh.j2"
_FRESH_SH_TPL = _TEMPLATES_DIR / "wiki-compile-freshness.sh.j2"
_ALERT_SVC_TPL = _TEMPLATES_DIR / "cron-failure-alert@.service.j2"
_FRESH_SVC_TPL = _TEMPLATES_DIR / "wiki-compile-freshness.service.j2"
_FRESH_TIMER_TPL = _TEMPLATES_DIR / "wiki-compile-freshness.timer.j2"

_SCRIPT_DIR = "/home/claude/scripts"
_ALERT_SH = "/home/claude/scripts/cron-failure-alert.sh"
_FRESH_SH = "/home/claude/scripts/wiki-compile-freshness.sh"
_ALERT_SVC = "/etc/systemd/system/cron-failure-alert@.service"
_FRESH_SVC = "/etc/systemd/system/wiki-compile-freshness.service"
_FRESH_TIMER = "/etc/systemd/system/wiki-compile-freshness.timer"
_COMPILE_DROPIN_DIR = "/etc/systemd/system/cloud-wiki-compile@.service.d"
_COMPILE_DROPIN = f"{_COMPILE_DROPIN_DIR}/onfailure.conf"

_WIKI_COMPILE_LOG_DIR = "/home/claude/logs/bubble-wiki"
_MAX_AGE_SEC = 26 * 3600  # nightly (24h); 26h = one missed run + slack


def _inline_dropin() -> Path:
    """OnFailure drop-in (3 lines, no variables) -> temp file for files.put."""
    content = (
        "# Managed by bubble-vps-platform -- wire the compile service to the\n"
        "# generic Telegram failure alerter.\n"
        "[Unit]\n"
        "OnFailure=cron-failure-alert@%n.service\n"
    )
    fd = tempfile.NamedTemporaryFile(
        mode="w", suffix="-onfailure.conf", delete=False, encoding="utf-8"
    )
    fd.write(content)
    fd.close()
    return Path(fd.name)


def apply() -> None:
    """Install the wiki-compile alerting (OnFailure + freshness watchdog)."""
    cfg = get_tenant_config(host)

    s = cfg.secrets
    if s is None or not s.enabled:
        return
    decrypted_runtime_path = s.decrypted_runtime_path

    operator_telegram_user_id = cfg.contact.primary_telegram_user_id
    if not operator_telegram_user_id:
        return

    # 1. /home/claude/scripts/ exists (claude-owned)
    files.directory(
        name="monitoring/wiki_compile_alerts: ensure /home/claude/scripts/",
        path=_SCRIPT_DIR, present=True, mode="0755",
        user="claude", group="claude", _sudo=True, _sudo_user="claude",
    )

    # 2. Render the two alert scripts (claude-owned)
    files.template(
        name=f"monitoring/wiki_compile_alerts: render {_ALERT_SH}",
        src=str(_ALERT_SH_TPL), dest=_ALERT_SH, mode="0755",
        user="claude", group="claude",
        decrypted_runtime_path=decrypted_runtime_path,
        operator_telegram_user_id=operator_telegram_user_id,
        _sudo=True, _sudo_user="claude",
    )
    files.template(
        name=f"monitoring/wiki_compile_alerts: render {_FRESH_SH}",
        src=str(_FRESH_SH_TPL), dest=_FRESH_SH, mode="0755",
        user="claude", group="claude",
        decrypted_runtime_path=decrypted_runtime_path,
        operator_telegram_user_id=operator_telegram_user_id,
        wiki_compile_log_dir=_WIKI_COMPILE_LOG_DIR, max_age_sec=_MAX_AGE_SEC,
        _sudo=True, _sudo_user="claude",
    )

    # 3. Drop systemd units (root-owned)
    alert_svc_op = files.template(
        name=f"monitoring/wiki_compile_alerts: drop {_ALERT_SVC}",
        src=str(_ALERT_SVC_TPL), dest=_ALERT_SVC, mode="0644",
        user="root", group="root",
        decrypted_runtime_path=decrypted_runtime_path, _sudo=True,
    )
    fresh_svc_op = files.template(
        name=f"monitoring/wiki_compile_alerts: drop {_FRESH_SVC}",
        src=str(_FRESH_SVC_TPL), dest=_FRESH_SVC, mode="0644",
        user="root", group="root",
        decrypted_runtime_path=decrypted_runtime_path, _sudo=True,
    )
    fresh_timer_op = files.template(
        name=f"monitoring/wiki_compile_alerts: drop {_FRESH_TIMER}",
        src=str(_FRESH_TIMER_TPL), dest=_FRESH_TIMER, mode="0644",
        user="root", group="root", _sudo=True,
    )

    # 4. OnFailure drop-in on the compile service
    files.directory(
        name="monitoring/wiki_compile_alerts: ensure compile drop-in dir",
        path=_COMPILE_DROPIN_DIR, present=True, mode="0755",
        user="root", group="root", _sudo=True,
    )
    dropin_op = files.put(
        name=f"monitoring/wiki_compile_alerts: drop {_COMPILE_DROPIN}",
        src=str(_inline_dropin()), dest=_COMPILE_DROPIN, mode="0644",
        user="root", group="root", _sudo=True,
    )

    # 5. daemon-reload (gated on any unit change)
    server.shell(
        name="monitoring/wiki_compile_alerts: daemon-reload (only if units changed)",
        commands=["systemctl daemon-reload"],
        _if=lambda: (
            alert_svc_op.did_change() or fresh_svc_op.did_change()
            or fresh_timer_op.did_change() or dropin_op.did_change()
        ),
        _sudo=True,
    )

    # 6. Enable + start the freshness timer
    systemd.service(
        name="monitoring/wiki_compile_alerts: enable + start freshness timer",
        service="wiki-compile-freshness.timer",
        enabled=True, running=True, _sudo=True,
    )
    server.shell(
        name="monitoring/wiki_compile_alerts: restart timer (only if changed)",
        commands=["systemctl restart wiki-compile-freshness.timer"],
        _if=fresh_timer_op.did_change, _sudo=True,
    )
