"""pyinfra inventory for the Bubble VPS Platform.

Reads tenant configs from `bubble-vps-data` (sibling repo by default) and
exposes them as pyinfra inventory groups.

Selection:
    TENANT=<name>     deploy a single tenant
    TENANTS_ALL=1     deploy ALL tenants
    BUBBLE_DATA_REPO  override the data repo path (default: ../bubble-vps-data)

Exits 2 if neither TENANT nor TENANTS_ALL is set, or if BUBBLE_DATA_REPO
does not exist. Exits 3 if any selected tenant.yaml fails validation.

Per SPEC-002, this is the fail-fast gate: validation runs BEFORE any SSH
connection. pyinfra reads the module-level group variables (tuples of
(host, host_data)) and connects via the ssh connector.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `lib.tenant_loader` importable when pyinfra exec's this file.
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.tenant_loader import (  # noqa: E402
    TenantConfig,
    TenantConfigError,
    load_tenant,
)


def _die(msg: str, code: int) -> None:
    print(f"# bubble-vps-platform inventory: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _resolve_data_repo() -> Path:
    raw = os.environ.get("BUBBLE_DATA_REPO")
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = (_REPO_ROOT / ".." / "bubble-vps-data").resolve()
    if not path.is_dir():
        _die(
            f"BUBBLE_DATA_REPO does not exist or is not a directory: {path}. "
            f"Set BUBBLE_DATA_REPO=/path/to/bubble-vps-data or clone the data "
            f"repo as a sibling of bubble-vps-platform.",
            2,
        )
    if not (path / "tenants").is_dir():
        _die(
            f"BUBBLE_DATA_REPO has no tenants/ directory: {path}",
            2,
        )
    return path


def _resolve_selection(data_repo: Path) -> list[str]:
    tenant_env = os.environ.get("TENANT", "").strip()
    all_env = os.environ.get("TENANTS_ALL", "").strip()

    if tenant_env and all_env == "1":
        _die(
            "Both TENANT and TENANTS_ALL=1 are set; pick one.",
            2,
        )
    if not tenant_env and all_env != "1":
        _die(
            "TENANT environment variable required (e.g. TENANT=bubble-internal). "
            "Or set TENANTS_ALL=1 to deploy every tenant in the data repo. "
            "Refusing to deploy without explicit selection.",
            2,
        )

    if tenant_env:
        return [tenant_env]

    # TENANTS_ALL=1: every dir under tenants/ that has a tenant.yaml.
    names: list[str] = []
    for child in sorted((data_repo / "tenants").iterdir()):
        if child.is_dir() and (child / "tenant.yaml").is_file():
            names.append(child.name)
    if not names:
        _die(f"TENANTS_ALL=1 set but no tenants found under {data_repo / 'tenants'}", 2)
    return names


def _build_host_entry(cfg: TenantConfig, data_repo: Path) -> tuple[str, dict]:
    """Return a (ssh_target, host_data) tuple for pyinfra.

    Per SPEC-003, host_data is intentionally MINIMAL: only what every task
    needs. Tasks that need full tenant config call lib.host_helpers.get_tenant_config()
    which loads it from disk on demand. This keeps PII (contact info, server IDs,
    secret-ref names, notes) out of pyinfra logs and crash dumps.
    """
    tenant_dir = data_repo / "tenants" / cfg.tenant_name
    persona_dir = (tenant_dir / cfg.agent.persona.persona_dir).resolve()
    secrets_file = (tenant_dir / "secrets.sops.env").resolve()  # may not exist yet (Step 3)

    # Use the IP as the connection target — safer than relying on /etc/hosts
    # or SSH config aliases on every operator machine.
    ssh_target = cfg.host.ip

    host_data = {
        # SSH connection params (required by pyinfra ssh connector)
        "ssh_user": cfg.host.ssh_user,
        "ssh_port": cfg.host.ssh_port,
        "ssh_hostname": cfg.host.ip,
        # Small per-host identifiers (used for templating, gating internal-only tasks)
        "tenant_name": cfg.tenant_name,
        "tenant_type": cfg.tenant_type,
        # Operator-Mac-side paths (resolved here to avoid recomputing per task)
        "persona_dir": str(persona_dir),
        "secrets_file": str(secrets_file),
        # NOTE: full tenant_config is intentionally NOT exposed here. See SPEC-003.
        # Tasks that need it call lib.host_helpers.get_tenant_config(host).
    }
    return (ssh_target, host_data)


# ─── Build inventory ────────────────────────────────────────────────────────

_DATA_REPO = _resolve_data_repo()
_SELECTED_NAMES = _resolve_selection(_DATA_REPO)

_LOADED: list[TenantConfig] = []
for _name in _SELECTED_NAMES:
    try:
        _LOADED.append(load_tenant(_name, _DATA_REPO))
    except TenantConfigError as exc:
        _die(f"tenant {_name!r} failed validation: {exc}", 3)

_ENTRIES: list[tuple[str, dict]] = [_build_host_entry(c, _DATA_REPO) for c in _LOADED]

# pyinfra reads module-level group variables of type list (or tuple of
# (hosts, group_data)). We use lists of (host, host_data) tuples so each
# entry carries its per-host data dict — matches SPEC-002 §"File: inventory.py".
_LINUX = [e for e, c in zip(_ENTRIES, _LOADED) if c.host.os_family == "linux"]
_MACOS = [e for e, c in zip(_ENTRIES, _LOADED) if c.host.os_family == "macos"]
_INTERNAL = [e for e, c in zip(_ENTRIES, _LOADED) if c.tenant_type == "internal"]
_CLIENT = [e for e, c in zip(_ENTRIES, _LOADED) if c.tenant_type == "client"]

# Summary line (helps debug; goes to stderr so it doesn't pollute pyinfra output)
_summary_tenants = ", ".join(_SELECTED_NAMES)
print(
    f"# bubble-vps-platform inventory: {len(_ENTRIES)} host(s) selected "
    f"(tenant={_summary_tenants})",
    file=sys.stderr,
)

# Module-level inventory groups (pyinfra discovers these by name).
# Must be list-of-(host, data) tuples; a bare tuple at module level is
# interpreted by pyinfra as (hosts_list, group_data).
linux_hosts: list = _LINUX
macos_hosts: list = _MACOS
internal_hosts: list = _INTERNAL
client_hosts: list = _CLIENT
