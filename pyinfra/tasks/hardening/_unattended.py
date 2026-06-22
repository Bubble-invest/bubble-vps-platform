"""unattended-upgrades hardening sub-module.

Drops `/etc/apt/apt.conf.d/52unattended-upgrades-bubble` from a jinja2 template,
verifies that unattended-upgrades.service is enabled.

Drift discovery (2026-05-08):
    The file on {{VPS_HOST}} contains MORE than the spec sketch — it overrides
    Allowed-Origins (security-only origins, no `-updates`/`-proposed`),
    Package-Blacklist (empty), Automatic-Reboot=true, Automatic-Reboot-WithUsers=true,
    Automatic-Reboot-Time=04:00, plus three Remove-Unused-* directives.
    See pyinfra/templates/unattended-upgrades-bubble.j2 — we match reality.

    The Periodic::Update-Package-Lists / Periodic::Unattended-Upgrade lines
    from the spec sketch are deliberately NOT in the file because Ubuntu 24.04
    ships those defaults in /etc/apt/apt.conf.d/20auto-upgrades (set during
    `dpkg-reconfigure unattended-upgrades` at install time). Adding them here
    would be redundant — and worse, would create drift if Ubuntu changes the
    default file location.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra.operations import apt, files, systemd


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "unattended-upgrades-bubble.j2"
)
_REMOTE_PATH = "/etc/apt/apt.conf.d/52unattended-upgrades-bubble"


def apply(uu_cfg) -> None:
    """Apply unattended-upgrades hardening.

    Args:
        uu_cfg: UnattendedUpgradesConfig dataclass (enabled, auto_reboot_time).
    """
    # 1) Install unattended-upgrades — Ubuntu 24.04 minimal includes it but
    #    cloud images sometimes don't. Idempotent.
    apt.packages(
        name="hardening/unattended: install unattended-upgrades",
        packages=["unattended-upgrades"],
        present=True,
        update=False,
    )

    # 2) Drop the bubble override file.
    files.template(
        name="hardening/unattended: write /etc/apt/apt.conf.d/52unattended-upgrades-bubble",
        src=str(_TEMPLATE_PATH),
        dest=_REMOTE_PATH,
        user="root",
        group="root",
        mode="644",
        auto_reboot_time=(uu_cfg.auto_reboot_time or "04:00"),
    )

    # 3) Ensure the service is enabled. unattended-upgrades is a oneshot-ish
    #    service triggered by the apt-daily timer; it appears `active (exited)`
    #    after each run. We only enforce `enabled` here — `running` doesn't
    #    apply to oneshots.
    systemd.service(
        name="hardening/unattended: ensure unattended-upgrades.service enabled",
        service="unattended-upgrades.service",
        enabled=True,
    )
