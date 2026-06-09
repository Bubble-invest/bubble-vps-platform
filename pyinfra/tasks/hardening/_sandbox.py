"""Install OS sandbox (Layer B — anti prompt-injection).

Delegates to the battle-tested bubble-ops-loop/scripts/install-sandbox.sh
which installs bwrap+socat+AppArmor+@anthropic-ai/sandbox-runtime and
merges the sandbox block into /etc/claude-code/managed-settings.json.

Idempotent: safe to re-run. Verified live on Morty (all 5 agents).
"""
from pyinfra import host
from pyinfra.operations import server

OPS_LOOP = "/home/claude/bubble-ops-loop"


def apply() -> None:
    server.shell(
        name="Run install-sandbox.sh (idempotent)",
        commands=[f"bash {OPS_LOOP}/scripts/install-sandbox.sh"],
        _sudo=True,
    )
