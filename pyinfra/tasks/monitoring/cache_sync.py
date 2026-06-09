"""Install bubble-cache-sync timer + service (every 10min).

Delegates to the battle-tested bubble-ops-loop install script.
Idempotent: safe to re-run.
"""
from pyinfra import host
from pyinfra.operations import server

OPS_LOOP = "/home/claude/bubble-ops-loop"


def apply() -> None:
    server.shell(
        name="Run install-cache-sync.sh (idempotent)",
        commands=[f"bash {OPS_LOOP}/scripts/install-cache-sync.sh"],
        _sudo=True,
    )
