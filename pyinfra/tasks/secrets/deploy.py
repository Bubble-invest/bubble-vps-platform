"""Secrets layer — public entrypoint (SPEC-006 + SPEC-008).

Reads cfg.secrets from tenant.yaml. If `enabled=False` (or the section is
absent), this is a no-op — secrets layer is opt-in per-tenant.

PHASE D (FIRST HALF):
    1. _binaries.apply() — installs age + sops on the box
    2. _age_setup.apply() — generates per-tenant box age keypair if missing,
                            copies pubkey BACK to operator Mac

PHASE D (SECOND HALF — SPEC-008):
    3. _sops_deploy.apply() — uploads the encrypted secrets blob to
                              /etc/bubble/secrets.sops.env, verifies on-box
                              decryption (exit code only — no plaintext
                              ever echoed), and validates required_keys are
                              present in the decrypted output.

This step assumes the operator has already added the box pubkey to
`.sops.yaml` and run `sops updatekeys` so the box's age key is a recipient
of the encrypted file. If not, _sops_deploy's verification step will fail
loud (sops --decrypt returns non-zero) and abort the deploy.

Mirror's hardening/linux.py's invocation pattern: importable as a module
(when called from deploy.py), or runnable as a deploy file (pyinfra exec's
it directly, no __package__).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pin the platform repo root and the local pyinfra/ dir on sys.path so absolute
# imports work regardless of how pyinfra invokes us. Same pattern as
# tasks/hardening/linux.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TASKS_ROOT = Path(__file__).resolve().parents[2]  # bubble-vps-platform/pyinfra/
for p in (str(_REPO_ROOT), str(_TASKS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pyinfra import host  # noqa: E402

from lib.host_helpers import get_tenant_config  # noqa: E402

from tasks.secrets import _age_setup, _binaries, _sops_deploy  # noqa: E402


def apply() -> None:
    """Apply the secrets layer to the current host (Phase D, both halves)."""
    cfg = get_tenant_config(host)
    s = cfg.secrets

    # Opt-out: no secrets block, or explicitly disabled = nothing to do.
    if s is None or not s.enabled:
        return

    # Order matters:
    #   1) install age + sops (age-keygen is needed for the next step)
    #   2) generate the per-tenant box keypair (so .sops.yaml can add it as
    #      a recipient — this is the manual gate the operator passes through)
    #   3) ship the encrypted blob + verify on-box decryption
    _binaries.apply()
    _age_setup.apply()
    _sops_deploy.apply()


# When pyinfra exec's this file as a deploy script directly, fire apply().
# Same detection trick as tasks/hardening/linux.py: pyinfra's exec_file uses
# `exec(code, data)` with no __package__ in globals, while normal imports set
# it to "tasks.secrets".
if not globals().get("__package__"):
    apply()
