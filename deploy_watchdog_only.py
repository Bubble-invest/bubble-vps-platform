"""Single-task deploy: telegram_watchdog ONLY.

Used to push the fixed telegram-watchdog (SPEC-021: bot.pid health signal,
cgroup-scoped poller check, stop->start recovery, 401->alert) to an existing
tenant's concierge WITHOUT re-running the full stack (hardening/secrets/agent).
Path setup mirrors deploy.py so `from tasks...` resolves under pyinfra exec.

Usage:
    TENANT=bubble-internal .venv/bin/pyinfra inventory.py deploy_watchdog_only.py --dry
    TENANT=bubble-internal .venv/bin/pyinfra inventory.py deploy_watchdog_only.py -y
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_TASKS_ROOT = _REPO_ROOT / "pyinfra"
for p in (str(_REPO_ROOT), str(_TASKS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tasks.access import telegram_watchdog  # noqa: E402

telegram_watchdog.apply()
