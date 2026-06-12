"""Shared pytest config for the lib/ suite.

Some tests depend on resources that exist on a dev box / the VPS but not on a
bare Linux CI runner. They pass where the resource is present and must SKIP
cleanly (not error) where it is absent. Three such resources:

  1. The sibling `bubble-vps-data` repo (a LOCAL-ONLY data repo — intentionally
     never pushed to GitHub, so CI cannot check it out). Golden-file / live-tenant
     tests load the real `bubble-internal` tenant from it.
  2. macOS Keychain (`/usr/bin/security`) — the operator-set-keychain script.
  3. `sops` on PATH (+ the real .sops.yaml, which lives in the data repo) — the
     new-tenant SOPS round-trip tests.

Everything that builds its own tmp fixtures runs everywhere. None of these are
regressions — the gated tests are OS/tooling/data specific by design.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# 1) sibling data repo
_DATA_REPO = (Path(__file__).resolve().parent.parent / ".." / "bubble-vps-data").resolve()
_DATA_PRESENT = (_DATA_REPO / "tenants" / "bubble-internal" / "tenant.yaml").is_file()
# 2) macOS Keychain  3) sops on PATH
_HAS_MACOS_SECURITY = Path("/usr/bin/security").exists()
_HAS_SOPS = shutil.which("sops") is not None

# Modules that load the real bubble-internal tenant (need the data repo).
_DATA_DEPENDENT_MODULES = {
    "test_agent_layer", "test_agent_os_user", "test_cloud_wiki_sync",
    "test_dashboard", "test_hardening_templates", "test_phone_home",
    "test_offboard_tenant_script", "test_host_data_exposure",
    "test_operator_set_secret_sh", "test_security_audit", "test_telegram_watchdog",
}
# Individual data-dependent tests inside otherwise-tmp-fixture modules.
_DATA_DEPENDENT_TESTS = {
    "test_load_valid_tenant_internal",
    "test_bubble_internal_loads_with_claudette_git_backed",
    "test_secrets_config_parses",
}
# Modules requiring macOS Keychain (`security`).
_MACOS_MODULES = {"test_operator_set_keychain_sh"}
# Modules requiring sops installed + the real .sops.yaml (data repo).
_SOPS_MODULES = {"test_new_tenant_script"}


def pytest_collection_modifyitems(config, items):
    def _skip(reason):
        return pytest.mark.skip(reason=reason)

    for item in items:
        module_stem = Path(str(item.fspath)).stem
        base_name = item.name.split("[", 1)[0]

        if not _DATA_PRESENT and (
            module_stem in _DATA_DEPENDENT_MODULES or base_name in _DATA_DEPENDENT_TESTS
        ):
            item.add_marker(_skip(
                "needs the sibling bubble-vps-data repo (local-only, not on "
                "GitHub) — present on a dev box / the VPS, absent in CI"))
            continue
        if module_stem in _MACOS_MODULES and not _HAS_MACOS_SECURITY:
            item.add_marker(_skip("needs macOS Keychain (/usr/bin/security) — not on a Linux CI runner"))
            continue
        if module_stem in _SOPS_MODULES and (not _HAS_SOPS or not _DATA_PRESENT):
            item.add_marker(_skip("needs `sops` installed + the real .sops.yaml (data repo) — absent in CI"))
            continue
