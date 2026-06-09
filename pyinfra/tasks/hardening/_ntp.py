"""NTP/chrony hardening sub-module.

Installs chrony, ensures the chrony daemon is running and enabled.

Note (parent decision 2026-05-08):
    chrony is added even though the manual hardening on 2026-05-06 left
    Ubuntu's default `systemd-timesyncd` in place. The first dogfood run
    against joris-cx33 will therefore report ONE change (chrony install).
    Subsequent runs will be clean. This is intentional — drift-free time
    is a basic requirement for a hardened production box, and chrony has
    better stratum-NTP behaviour than timesyncd.
"""

from __future__ import annotations

from pyinfra.operations import apt, systemd


def apply() -> None:
    """Install + enable chrony.

    No tenant.yaml knob — chrony is always-on.
    """
    apt.packages(
        name="hardening/ntp: install chrony",
        packages=["chrony"],
        present=True,
        update=False,
    )

    # chrony.service ships enabled+running by default after install on Ubuntu,
    # but we declare it explicitly so the playbook also fixes drift if a future
    # operator manually disables it.
    systemd.service(
        name="hardening/ntp: ensure chrony.service running+enabled",
        service="chrony.service",
        running=True,
        enabled=True,
    )
