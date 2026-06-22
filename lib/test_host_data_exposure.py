"""Tests for SPEC-003 (host data exposure policy).

Per SPEC-003, inventory.py's host_data must be MINIMAL — no full tenant_config
dict, no contact info, no notes, no secret-ref names. Tasks that need the
full config call lib.host_helpers.get_tenant_config(host).

Run with: python3.12 -m pytest lib/test_host_data_exposure.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# ─── Allowed keys per SPEC-003 ─────────────────────────────────────────────

ALLOWED_HOST_DATA_KEYS = {
    "ssh_user",
    "ssh_port",
    "ssh_hostname",
    "tenant_name",
    "tenant_type",
    "persona_dir",
    "secrets_file",
}

# Field PATTERNS that must NOT appear anywhere in host_data when serialized.
# NOTE: the contact/host values below are placeholders for the OSS repo. On a
# dev box these must match the real bubble-internal values in the private
# bubble-vps-data repo for the negative-needle check to be meaningful — keep
# them in sync with that repo (these tests skip when the data repo is absent).
PII_PATTERNS = [
    "operator@example.com",  # contact.primary_email (placeholder)
    "100000001",              # contact.primary_telegram_user_id (placeholder)
    "99999999",               # host.provider_server_id (placeholder)
    "OPENROUTER_API_KEY",      # secret_ref name (sensitive as relationship metadata)
    "TELEGRAM_BOT_TOKEN",      # secret_ref name
    "TAILSCALE_AUTHKEY",       # secret_ref name
    "deepseek/deepseek",       # llm.model — agent fingerprint
    "openrouter.ai",           # llm.base_url
]


# ─── Fixtures ──────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inventory_module(monkeypatch):
    """Load inventory.py as a module with the bubble-internal tenant selected.

    inventory.py is normally exec'd by pyinfra, not imported. We import it
    here and re-import per-test (importlib.reload) so each test gets a fresh
    state. Returns the loaded module.
    """
    import importlib

    # Ensure the repo root is on sys.path
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    monkeypatch.setenv("TENANT", "bubble-internal")
    monkeypatch.setenv(
        "BUBBLE_DATA_REPO",
        str((REPO_ROOT / ".." / "bubble-vps-data").resolve()),
    )

    # Fresh import each time
    if "inventory" in sys.modules:
        del sys.modules["inventory"]
    inv = importlib.import_module("inventory")
    return inv


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_host_data_only_contains_allowed_keys(inventory_module):
    """SPEC-003: host.data must contain ONLY the explicitly allowed keys."""
    assert len(inventory_module.linux_hosts) == 1, "expected exactly bubble-internal host"

    _, host_data = inventory_module.linux_hosts[0]
    actual_keys = set(host_data.keys())

    extra = actual_keys - ALLOWED_HOST_DATA_KEYS
    missing = ALLOWED_HOST_DATA_KEYS - actual_keys

    assert not extra, f"host_data contains forbidden keys: {extra}"
    assert not missing, f"host_data is missing required keys: {missing}"


def test_host_data_does_not_contain_tenant_config_dict(inventory_module):
    """SPEC-003 §Policy: 'host.data MUST NOT contain Full tenant config dict'."""
    _, host_data = inventory_module.linux_hosts[0]
    assert "tenant_config" not in host_data
    assert "raw" not in host_data
    assert "config" not in host_data


def test_host_data_serialized_does_not_leak_pii(inventory_module):
    """SPEC-003 threat model: dumping host.data via repr/json must not expose PII."""
    _, host_data = inventory_module.linux_hosts[0]

    # Cover the two ways pyinfra typically logs host.data
    repr_dump = repr(host_data)
    json_dump = json.dumps(host_data)

    for pattern in PII_PATTERNS:
        assert pattern not in repr_dump, (
            f"PII leak in repr(host_data): pattern {pattern!r} found"
        )
        assert pattern not in json_dump, (
            f"PII leak in json.dumps(host_data): pattern {pattern!r} found"
        )


def test_host_data_contains_minimal_identifiers(inventory_module):
    """Sanity check: tenant_name and ssh params ARE in host_data (allowed)."""
    _, host_data = inventory_module.linux_hosts[0]
    assert host_data["tenant_name"] == "bubble-internal"
    assert host_data["tenant_type"] == "internal"
    assert host_data["ssh_user"] == "claude"
    assert host_data["ssh_port"] == 22
    assert host_data["ssh_hostname"] == "203.0.113.10"


def test_get_tenant_config_helper_returns_full_config():
    """SPEC-003 access pattern: tasks load full config via get_tenant_config()."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    os.environ["BUBBLE_DATA_REPO"] = str(
        (REPO_ROOT / ".." / "bubble-vps-data").resolve()
    )

    from lib.host_helpers import get_tenant_config

    # Mock pyinfra's host object — only host.data.tenant_name is read
    class MockHostData:
        tenant_name = "bubble-internal"

    class MockHost:
        data = MockHostData()

    cfg = get_tenant_config(MockHost())
    assert cfg.tenant_name == "bubble-internal"
    # Verify the full config IS available via the helper (the whole point)
    assert cfg.contact.primary_email == "operator@example.com"
    assert cfg.host.provider_server_id == "99999999"
    # bubble-internal uses Claude Code subscription, not API key auth
    assert cfg.agent.llm.auth_mode == "claude_code_subscription"
    assert cfg.agent.llm.api_key_secret_ref is None


def test_persona_dir_path_resolved_correctly(inventory_module):
    """persona_dir is operator-Mac-side absolute path under data repo."""
    _, host_data = inventory_module.linux_hosts[0]
    persona_dir = Path(host_data["persona_dir"])
    assert persona_dir.is_absolute()
    assert persona_dir.exists(), f"persona_dir does not exist: {persona_dir}"
    # Step 5a (SPEC-010): persona renamed ricky → morty.
    assert persona_dir.name == "morty"
    assert (persona_dir / "CLAUDE.md").exists()


def test_secrets_file_path_resolved_correctly(inventory_module):
    """secrets_file path is computed correctly even though file doesn't exist yet (Step 1)."""
    _, host_data = inventory_module.linux_hosts[0]
    secrets_file = Path(host_data["secrets_file"])
    assert secrets_file.is_absolute()
    assert secrets_file.name == "secrets.sops.env"
    assert "bubble-internal" in str(secrets_file)
    # Existence is OPTIONAL at this step (Step 1 — secrets layer is Step 3)
