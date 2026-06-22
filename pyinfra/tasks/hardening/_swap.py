"""Swap hardening sub-module.

Three-step:
    1. Create swapfile if missing (guarded with `test -f /swapfile`).
    2. Persist `/swapfile none swap sw 0 0` line in `/etc/fstab`.
    3. Set `vm.swappiness=10` in `/etc/sysctl.d/99-bubble-swap.conf`. Reload
       sysctl only if the file changed.

Drift discovery (2026-05-08):
    The sysctl file on {{VPS_HOST}} is `/etc/sysctl.d/99-bubble-swap.conf`
    (NOT `99-bubble-platform.conf` as parent agent decision suggested). The
    content is also unspaced — `vm.swappiness=10` not `vm.swappiness = 10`.
    Both formats are valid sysctl syntax but we match the file on the box
    byte-for-byte so the dogfood reports zero changes.
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.facts.files import File
from pyinfra.facts.server import Command
from pyinfra.operations import files, server


_SYSCTL_FILE = "/etc/sysctl.d/99-bubble-swap.conf"


def apply(swap_cfg) -> None:
    """Apply swap hardening.

    Args:
        swap_cfg: SwapConfig (enabled, size_gb, swappiness).
    """
    size_gb = swap_cfg.size_gb if swap_cfg.size_gb is not None else 2
    swappiness = swap_cfg.swappiness if swap_cfg.swappiness is not None else 10

    # 1) Create the swapfile only if missing. The shell guard makes the whole
    #    sequence a no-op when /swapfile exists (which it does on {{VPS_HOST}}).
    #    We use host.get_fact(File, ...) to avoid even running the shell op
    #    on subsequent runs — that keeps the pyinfra "Changed" count clean.
    swapfile_present = host.get_fact(File, path="/swapfile") is not None

    if not swapfile_present:
        server.shell(
            name=f"hardening/swap: create {size_gb}GB swapfile",
            commands=[
                f"fallocate -l {size_gb}G /swapfile && "
                f"chmod 600 /swapfile && "
                f"mkswap /swapfile && "
                f"swapon /swapfile"
            ],
        )

    # 2) Persist the swapfile mount in /etc/fstab. files.line uses grep+sed,
    #    so it's idempotent: appends only if the line is absent. The `line`
    #    arg is treated as a regex, so the literal whitespace must match.
    files.line(
        name="hardening/swap: persist /swapfile in /etc/fstab",
        path="/etc/fstab",
        line="/swapfile none swap sw 0 0",
    )

    # 3) Drop the sysctl swappiness file. files.put hashes content vs remote
    #    and is a no-op when they match.
    sysctl_content = f"vm.swappiness={swappiness}\n".encode()
    from io import BytesIO

    sysctl_op = files.put(
        name=f"hardening/swap: write {_SYSCTL_FILE}",
        src=BytesIO(sysctl_content),
        dest=_SYSCTL_FILE,
        user="root",
        group="root",
        mode="644",
    )

    # 4) Apply the sysctl change ONLY if the file just changed. Otherwise it's
    #    already loaded (sysctl files are auto-applied at boot via systemd-sysctl).
    server.shell(
        name="hardening/swap: reload sysctl swappiness",
        commands=[f"sysctl -p {_SYSCTL_FILE}"],
        _if=sysctl_op.did_change,
    )
