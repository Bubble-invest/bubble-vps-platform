"""Install secrets-tmp-sweep timer + service (every 30min).

Delegates to the battle-tested bubble-ops-loop install script.
Idempotent: safe to re-run.
"""
from pyinfra import host
from pyinfra.operations import server

OPS_LOOP = "/home/claude/bubble-ops-loop"


def apply() -> None:
    server.shell(
        name="Run install-secrets-sweep.sh (idempotent)",
        commands=[f"bash {OPS_LOOP}/scripts/install-secrets-sweep.sh"],
        _sudo=True,
    )
