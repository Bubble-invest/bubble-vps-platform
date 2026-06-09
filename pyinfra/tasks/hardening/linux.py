"""Linux hardening — public entrypoint.

Per SPEC-005 §"Public API". Reads cfg.hardening from tenant.yaml (via
lib.host_helpers.get_tenant_config(host)) and dispatches to each sub-module.

Each sub-module is responsible for its own idempotency. The litmus test:
running this against an already-hardened joris-cx33 must report zero changes.
See tests/integration/test_dogfood_hardening.sh.

This module can be invoked TWO ways:
    1. As a deploy script directly:
       `pyinfra inventory.py pyinfra/tasks/hardening/linux.py`
    2. From deploy.py, which calls `apply()` after performing its own setup.

When pyinfra exec's a deploy file it uses `compile() + exec()` with no
__package__, so relative imports don't work. We do absolute imports of the
sub-modules instead, after pinning the platform repo on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pin two roots on sys.path:
#   - the platform repo root (so `lib.*` resolves)
#   - the local `pyinfra/` directory (so our `tasks.hardening.*` resolves
#     without colliding with the installed `pyinfra` package — the upstream
#     package shadows our `pyinfra/` directory if we tried to import it as
#     `pyinfra.tasks.*`).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_ROOT = Path(__file__).resolve().parents[2]  # bubble-vps-platform/pyinfra/
for p in (str(_REPO_ROOT), str(_TASKS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pyinfra import host  # noqa: E402  (the installed pyinfra package)
from pyinfra.operations import apt  # noqa: E402

from lib.host_helpers import get_tenant_config  # noqa: E402

# Absolute imports of sibling sub-modules under our local `tasks/` package.
# This works because we put _TASKS_ROOT on sys.path above.
from tasks.hardening import (  # noqa: E402
    _fail2ban,
    _ntp,
    _sandbox,
    _sshd,
    _swap,
    _unattended,
    _ufw,
)


def apply() -> None:
    """Apply the full Linux hardening profile to the current host.

    Reads cfg.hardening fresh from disk (per SPEC-003 — full config is NOT
    in host.data). Each sub-module is independently idempotent.
    """
    cfg = get_tenant_config(host)
    h = cfg.hardening

    # Refresh apt indexes once per deploy (cached for an hour). All sub-modules
    # below use `update=False` on apt.packages so they don't redundantly hit
    # the network. Per parent decision 2026-05-08.
    apt.update(
        name="hardening: refresh apt cache (cache_time=3600)",
        cache_time=3600,
    )

    # Order: sshd FIRST (the most fragile — if we lock ourselves out we want
    # to know immediately, before piling on more changes). UFW + fail2ban next.
    # NTP, swap, and unattended-upgrades are non-critical; order between them
    # is arbitrary.
    if h.sshd is not None:
        _sshd.apply(h.sshd)

    if h.ufw is not None and h.ufw.enabled:
        _ufw.apply(h.ufw)

    if h.fail2ban is not None and h.fail2ban.enabled:
        _fail2ban.apply(h.fail2ban)

    if h.unattended_upgrades is not None and h.unattended_upgrades.enabled:
        _unattended.apply(h.unattended_upgrades)

    if h.swap is not None and h.swap.enabled:
        _swap.apply(h.swap)

    # OS sandbox (Layer B) — applied unless explicitly disabled. Like NTP it is
    # table-stakes: every agent should be jailed. `h.sandbox` may be None
    # (absent from tenant.yaml) → the module applies the default fleet posture.
    sandbox_cfg = getattr(h, "sandbox", None)
    if sandbox_cfg is None or getattr(sandbox_cfg, "enabled", None) is not False:
        _sandbox.apply(sandbox_cfg)

    # NTP/chrony is always applied — drift-free time is table-stakes.
    _ntp.apply()


# When pyinfra runs this file as a deploy script directly, the module-level
# code is executed once per host in the operation context. Calling apply()
# at module top-level handles that mode. When deploy.py imports us, the same
# call would fire too — so deploy.py should NOT import this module; it should
# `import pyinfra.tasks.hardening.linux` only via this side-effect path, OR
# import the apply function from a non-side-effecting wrapper.
#
# Detection: pyinfra's exec_file uses `exec(code, data)` where data starts as
# {"__file__": filename}. __package__ is not in that dict, so globals().get
# returns None. When imported as `pyinfra.tasks.hardening.linux` via Python's
# import system, __package__ is "pyinfra.tasks.hardening".
if not globals().get("__package__"):
    apply()

# 8) OS sandbox (Layer B — bwrap + AppArmor + managed-settings sandbox block)
_sandbox.apply()
