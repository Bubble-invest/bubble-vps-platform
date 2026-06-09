"""Install Restic backup timer + service (every 6h).

Delegates to the battle-tested bubble-ops-loop install script.
Idempotent: safe to re-run.
"""
from pyinfra import host
from pyinfra.operations import server

OPS_LOOP = "/home/claude/bubble-ops-loop"


def apply() -> None:
    server.shell(
        name="Run morty-restic-setup.sh (idempotent)",
        commands=[f"bash {OPS_LOOP}/scripts/morty-restic-setup.sh"],
        _sudo=True,
    )
