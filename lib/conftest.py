"""Shared pytest config for the lib/ suite.

Some tests render golden files / validate against the REAL `bubble-internal`
tenant, which lives in a sibling repo `bubble-vps-data` (a LOCAL-ONLY data repo —
it is intentionally never pushed to GitHub, so CI cannot check it out). Those
tests pass on a dev box / the VPS where the sibling is present, and must SKIP
cleanly (not error) where it is absent (e.g. GitHub Actions). Everything that
builds its own tmp fixtures runs everywhere.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# The sibling data repo, resolved the same way the tests resolve it.
_DATA_REPO = (Path(__file__).resolve().parent.parent / ".." / "bubble-vps-data").resolve()
_DATA_PRESENT = (_DATA_REPO / "tenants" / "bubble-internal" / "tenant.yaml").is_file()

# Test modules that load the real bubble-internal tenant (golden-file / live
# tenant validations). When the data repo is absent they would raise
# TenantConfigError at fixture setup → collection ERROR. We skip them instead.
_DATA_DEPENDENT_MODULES = {
    "test_agent_layer",
    "test_agent_os_user",
    "test_cloud_wiki_sync",
    "test_dashboard",
    "test_hardening_templates",
    "test_phone_home",
    "test_offboard_tenant_script",
    "test_host_data_exposure",
    "test_operator_set_secret_sh",
    "test_security_audit",
    "test_telegram_watchdog",
}

# A few INDIVIDUAL tests in otherwise-tmp-fixture modules also load the real
# bubble-internal tenant. Skip these by exact node name (don't skip their
# module's many tmp-based tests).
_DATA_DEPENDENT_TESTS = {
    "test_load_valid_tenant_internal",
    "test_bubble_internal_loads_with_claudette_git_backed",
    "test_secrets_config_parses",
}


def pytest_collection_modifyitems(config, items):
    if _DATA_PRESENT:
        return  # data repo present → run everything (dev box / VPS)
    skip_marker = pytest.mark.skip(
        reason=(
            "requires the sibling bubble-vps-data repo (local-only, not on "
            "GitHub) — present on a dev box / the VPS, absent in CI"
        )
    )
    for item in items:
        module_stem = Path(str(item.fspath)).stem
        # item.name includes parametrize ids; match the base test function name.
        base_name = item.name.split("[", 1)[0]
        if module_stem in _DATA_DEPENDENT_MODULES or base_name in _DATA_DEPENDENT_TESTS:
            item.add_marker(skip_marker)
