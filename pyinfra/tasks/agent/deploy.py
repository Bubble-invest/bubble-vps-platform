"""Agent layer — public entrypoint (Step 4, SPEC-007 + SPEC-009).

Reads cfg.agent + cfg.secrets from tenant.yaml. Orchestrates the seven
sub-modules in this strict order:

    1. _install         → Node.js, Bun, Claude Code on the box
    2. _persona         → rsync persona content from data repo
    3. _settings        → write ~/.claude/settings.json (NO secrets)
    4. _telegram_plugin → ensure plugin runtime state dir exists
    5. _systemd_unit    → drop /etc/systemd/system/claude-agent-<persona>.service,
                          daemon-reload, enable, start, restart-on-change
    6. _verify          → SIX-CHECK GATE before cleanup runs (see _verify.py
                          docstring). Aborts the deploy if any check fails.
    7. _cleanup_legacy  → wipe the 8 plaintext-leak files. ONLY runs if
                          _verify passed (pyinfra short-circuits the deploy
                          on any prior op error).

The order matters. Specifically:
    - settings.json must be written BEFORE the systemd unit starts (the
      service reads ~/.claude/settings.json to know which plugins to load).
    - The systemd unit must be active BEFORE _verify checks for it.
    - _verify must pass BEFORE _cleanup_legacy deletes the rollback path.

If cfg.agent or cfg.secrets is opt-out, this is a no-op. The module is
safe to call against any tenant.

Same import-mode dispatch as tasks/secrets/deploy.py — pyinfra exec's deploy
files via compile()+exec() with no __package__, so we detect that and call
apply() at module top-level.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pin platform repo + local pyinfra/ on sys.path. Same dance as the hardening
# and secrets entrypoints — pyinfra's deploy-file exec doesn't set __package__,
# so absolute imports are the only way.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_REPO_ROOT), str(_TASKS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pyinfra import host  # noqa: E402

from lib.host_helpers import get_tenant_config  # noqa: E402

from tasks.agent import (  # noqa: E402
    _cleanup_legacy,
    _install,
    _persona,
    _settings,
    _systemd_unit,
    _telegram_plugin,
    _verify,
)


def apply() -> None:
    """Apply the agent layer to the current host."""
    cfg = get_tenant_config(host)
    if cfg.secrets is None or not cfg.secrets.enabled:
        # Step 4 cannot run without the secrets layer (the systemd unit
        # depends on /etc/bubble/secrets.sops.env existing + decryptable).
        return

    # Order: install → persona → settings → plugin → systemd → verify →
    # cleanup. Any earlier-step failure aborts the chain (pyinfra short-
    # circuits on op error).
    _install.apply()
    _persona.apply()
    _settings.apply()
    _telegram_plugin.apply()
    _systemd_unit.apply()
    _verify.apply()
    _cleanup_legacy.apply()


if not globals().get("__package__"):
    apply()
