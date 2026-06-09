"""Targeted deploy: agent layer + telegram watchdog ONLY (no hardening/secrets/tailscale/monitoring).

Used to deploy/refresh the per-concierge agent layer (persona clone-or-sync,
settings.json, systemd unit, telegram plugin, watchdog) without re-running the
full stack. Assumes the encrypted secrets blob is already present at
/etc/bubble/secrets.sops.env on the box (the agent units decrypt it at
ExecStartPre, so deploy-time secrets task is not strictly required when the
blob is already in place). Path setup mirrors deploy.py.

Usage:
    TENANT=bubble-internal BUBBLE_DATA_REPO=../bubble-vps-data \
        .venv/bin/pyinfra inventory.py deploy_agent_only.py --dry
    ... deploy_agent_only.py -y
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_TASKS_ROOT = _REPO_ROOT / "pyinfra"
for p in (str(_REPO_ROOT), str(_TASKS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tasks.agent import deploy as agent  # noqa: E402
from tasks.access import telegram_watchdog  # noqa: E402

agent.apply()
telegram_watchdog.apply()
