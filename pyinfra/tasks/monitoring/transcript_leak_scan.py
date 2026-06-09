"""Install transcript-leak-scan timer + service (daily 06:30 UTC).

Delegates to the battle-tested bubble-ops-loop install script.
Idempotent: safe to re-run.
"""
from pyinfra import host
from pyinfra.operations import server

OPS_LOOP = "/home/claude/bubble-ops-loop"


def apply() -> None:
    server.shell(
        name="Run install-transcript-leak-scan.sh (idempotent)",
        commands=[f"bash {OPS_LOOP}/scripts/install-transcript-leak-scan.sh"],
        _sudo=True,
    )
