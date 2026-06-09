"""fail2ban hardening sub-module.

Installs fail2ban, drops a configuration file from a jinja2 template, restarts
fail2ban only if the file changed.

Drift discovery (2026-05-08):
    The original SPEC-005 §_fail2ban.py says drop the config at
    `/etc/fail2ban/jail.d/bubble.conf`. On joris-cx33 the actual file is at
    `/etc/fail2ban/jail.local`. fail2ban reads BOTH locations, but jail.local
    is the historical convention and is what the manual hardening on 2026-05-06
    used. We match reality to keep dogfood at zero changes.

    The file format is also slightly different from the spec sketch — it does
    not include `{{ bans.maxretry }}` etc. inside the [DEFAULT] section in
    quite the same shape. The bubble-internal config uses (rendered with
    bubble-internal values):

        [DEFAULT]
        ignoreip = 127.0.0.1/8 ::1
        findtime = 10m
        maxretry = 5
        bantime = 1h
        backend = systemd

        [sshd]
        enabled = true
        port = 22
        mode = aggressive

        [recidive]
        enabled = true
        bantime = 1w
        findtime = 1d
        maxretry = 3

    See pyinfra/templates/fail2ban_bubble.conf.j2.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra.operations import apt, files, systemd


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "fail2ban_bubble.conf.j2"
)
_REMOTE_PATH = "/etc/fail2ban/jail.local"


def apply(f2b_cfg) -> None:
    """Apply fail2ban hardening.

    Args:
        f2b_cfg: Fail2banConfig dataclass (enabled, sshd_jail, bans).
    """
    # 1) Install fail2ban — idempotent.
    apt.packages(
        name="hardening/fail2ban: install fail2ban",
        packages=["fail2ban"],
        present=True,
        update=False,
    )

    # 2) Render the config. Defaults (from joris-cx33's manual hardening)
    #    apply when bans block is missing or has fields unset.
    bans = f2b_cfg.bans
    template_op = files.template(
        name="hardening/fail2ban: write /etc/fail2ban/jail.local",
        src=str(_TEMPLATE_PATH),
        dest=_REMOTE_PATH,
        user="root",
        group="root",
        mode="644",
        sshd_jail=(f2b_cfg.sshd_jail or "aggressive"),
        maxretry=((bans.maxretry if bans and bans.maxretry is not None else 5)),
        findtime_minutes=(
            bans.findtime_minutes if bans and bans.findtime_minutes is not None else 10
        ),
        bantime_hours=(
            bans.bantime_hours if bans and bans.bantime_hours is not None else 1
        ),
    )

    # 3) Restart fail2ban only if the config file changed. fail2ban needs a
    #    full restart (NOT reload) to pick up jail.local changes.
    systemd.service(
        name="hardening/fail2ban: restart fail2ban if config changed",
        service="fail2ban.service",
        restarted=True,
        _if=template_op.did_change,
    )

    # 4) Ensure running+enabled (idempotent — pyinfra checks current state).
    systemd.service(
        name="hardening/fail2ban: ensure fail2ban.service running+enabled",
        service="fail2ban.service",
        running=True,
        enabled=True,
    )
