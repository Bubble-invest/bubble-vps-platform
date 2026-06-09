"""Tests for scripts/new-tenant.sh — the tenant scaffolding bootstrapper.

Per SPEC-016 §"Test plan" — 8 tests. Uses pytest's tmp_path fixture for
isolation: every test points BUBBLE_DATA_REPO at a fresh temp dir and
copies in a minimal `.sops.yaml` so SOPS can find the recipient rule.

These tests DO require:
  - sops + age installed locally (the script invokes sops --encrypt)
  - SOPS_AGE_KEY_FILE present (the script needs a recipient to encrypt to)

If those aren't present, the relevant tests skip cleanly.

Run with: python3 -m pytest lib/test_new_tenant_script.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "new-tenant.sh"
DATA_REPO_REAL = REPO_ROOT.parent / "bubble-vps-data"
SOPS_YAML_REAL = DATA_REPO_REAL / ".sops.yaml"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _has_sops() -> bool:
    return shutil.which("sops") is not None


def _has_age_key() -> bool:
    key_file = os.environ.get(
        "SOPS_AGE_KEY_FILE", str(Path.home() / ".config/sops/age/keys.txt")
    )
    return Path(key_file).is_file()


def _make_tmp_data_repo(tmp_path: Path) -> Path:
    """Create a tmp_path-based data repo with the project's real .sops.yaml.

    The default .sops.yaml rule matches `tenants/[^/]+/secrets\\.sops\\.env$`
    and uses the operator master key. Copying the real one ensures we test
    against the actual production recipient config.
    """
    data_repo = tmp_path / "bubble-vps-data"
    (data_repo / "tenants").mkdir(parents=True)
    if SOPS_YAML_REAL.is_file():
        shutil.copy(SOPS_YAML_REAL, data_repo / ".sops.yaml")
    else:
        # Fallback minimal .sops.yaml — uses the operator master key from the
        # real .sops.yaml's default rule.
        (data_repo / ".sops.yaml").write_text(
            "creation_rules:\n"
            "  - path_regex: tenants/[^/]+/secrets\\.sops\\.env$\n"
            "    age: age1qal34hv5h99vvpq7kmghfz0mjh98eq9mj5dg5k43r8kwmumvnu5qt6w3hy\n"
        )
    return data_repo


def _run(args: list[str], data_repo: Path | None = None, timeout: int = 30):
    env = dict(os.environ)
    if data_repo is not None:
        env["BUBBLE_DATA_REPO"] = str(data_repo)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ─── Tests ──────────────────────────────────────────────────────────────────

def test_script_exists_and_executable():
    """Test 1: file exists, mode includes user-executable bit."""
    assert SCRIPT.is_file(), f"script not found at {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"script not executable: {SCRIPT}"


def test_script_rejects_no_args(tmp_path: Path):
    """Test 2: no positional arg → exit 2 with clear message."""
    data_repo = _make_tmp_data_repo(tmp_path)
    result = _run([], data_repo=data_repo)
    assert result.returncode == 2, (
        f"expected exit 2 (usage error), got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    msg = (result.stderr + result.stdout).lower()
    assert "tenant-name" in msg or "usage" in msg


def test_script_rejects_invalid_tenant_name(tmp_path: Path):
    """Test 3: uppercase or special chars → exit 2 with regex hint."""
    data_repo = _make_tmp_data_repo(tmp_path)

    # Uppercase
    result = _run(["BadName"], data_repo=data_repo)
    assert result.returncode == 2
    msg = (result.stderr + result.stdout).lower()
    assert "invalid tenant-name" in msg or "regex" in msg

    # Special chars (underscore)
    result = _run(["bad_name"], data_repo=data_repo)
    assert result.returncode == 2

    # Starts with digit
    result = _run(["1badname"], data_repo=data_repo)
    assert result.returncode == 2


@pytest.mark.skipif(
    not (_has_sops() and _has_age_key()),
    reason="needs sops + age key for encryption step",
)
def test_script_creates_directory_structure(tmp_path: Path):
    """Test 4: run with a temp BUBBLE_DATA_REPO, assert all expected files appear."""
    data_repo = _make_tmp_data_repo(tmp_path)
    result = _run(
        ["test-acme", "--type=client", "--display-name=Test Acme"],
        data_repo=data_repo,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    tenant_dir = data_repo / "tenants" / "test-acme"
    expected_files = [
        tenant_dir / "tenant.yaml",
        tenant_dir / "secrets.sops.env",
        tenant_dir / "README.md",
        tenant_dir / "persona" / "test-acme" / "CLAUDE.md",
        tenant_dir / "persona" / "test-acme" / "workspace" / "CLAUDE.md",
    ]
    for f in expected_files:
        assert f.is_file(), f"expected file missing: {f}"


@pytest.mark.skipif(
    not (_has_sops() and _has_age_key()),
    reason="needs sops + age key for encryption step",
)
def test_script_yaml_passes_loader_validation(tmp_path: Path):
    """Test 5: generated tenant.yaml passes through tenant_loader IF placeholders filled.

    Confirms the template generates a structurally valid skeleton — the only
    things missing are the operator-supplied values.
    """
    # Add the platform repo to sys.path so we can import lib.tenant_loader.
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from lib.tenant_loader import load_tenant_from_path

    data_repo = _make_tmp_data_repo(tmp_path)
    result = _run(
        ["test-acme", "--type=client", "--display-name=Test Acme"],
        data_repo=data_repo,
    )
    assert result.returncode == 0, result.stderr

    yaml_path = data_repo / "tenants" / "test-acme" / "tenant.yaml"
    content = yaml_path.read_text(encoding="utf-8")

    # Substitute placeholders with valid values (mimics what the operator
    # does after Hetzner provisioning + filling in contacts).
    # host.ip is the only PLACEHOLDER_FILL_AFTER_HETZNER_PROVISION that needs
    # to be a valid IPv4 — others (provider_server_id, region, firewall_id)
    # are string-typed and any non-empty value works.
    content = content.replace(
        "ip: PLACEHOLDER_FILL_AFTER_HETZNER_PROVISION", "ip: 1.2.3.4"
    )
    content = content.replace("\"PLACEHOLDER_TELEGRAM_USER_ID\"", "\"1234567\"")

    yaml_path.write_text(content, encoding="utf-8")

    # Should now validate (persona dir exists because the script created it).
    cfg = load_tenant_from_path(yaml_path, expected_name="test-acme")
    assert cfg.tenant_name == "test-acme"
    assert cfg.tenant_type == "client"
    assert cfg.display_name == "Test Acme"
    assert cfg.agent.persona.name == "test-acme"
    assert cfg.secrets is not None
    assert "TELEGRAM_BOT_TOKEN" in cfg.secrets.required_keys
    assert "PHONEHOME_TOKEN" in cfg.secrets.required_keys


@pytest.mark.skipif(
    not (_has_sops() and _has_age_key()),
    reason="needs sops + age key for encryption step",
)
def test_script_secrets_file_encrypted_with_correct_recipients(tmp_path: Path):
    """Test 6: generated secrets.sops.env has the operator master key as recipient.

    SOPS files are YAML-on-disk (even when encrypting dotenv content): the
    plaintext lines are replaced with `KEY=ENC[...]` ciphertexts but the
    `sops_age__list_X__map_recipient` metadata lives in trailing comments
    that yaml.safe_load can parse out of the `sops` top-level mapping.

    For dotenv format, sops actually appends a `#ENC[...]` comment block at
    the bottom containing the recipients in plain `recipient=...` lines.
    We grep the file for the operator master pubkey to confirm.
    """
    data_repo = _make_tmp_data_repo(tmp_path)
    result = _run(["test-acme"], data_repo=data_repo)
    assert result.returncode == 0, result.stderr

    secrets_file = data_repo / "tenants" / "test-acme" / "secrets.sops.env"
    assert secrets_file.is_file()
    content = secrets_file.read_text(encoding="utf-8")

    # Operator master pubkey from the real .sops.yaml default rule.
    OPERATOR_MASTER = "age1qal34hv5h99vvpq7kmghfz0mjh98eq9mj5dg5k43r8kwmumvnu5qt6w3hy"
    assert OPERATOR_MASTER in content, (
        "operator master pubkey not found in encrypted secrets file — "
        "either the .sops.yaml rule didn't match or sops didn't use the right recipient"
    )

    # Sanity: the placeholder values should be ENC[...]-wrapped (encrypted),
    # not present as plaintext.
    assert "PASTE_FROM_BOTFATHER" not in content, (
        "plaintext placeholder found in encrypted file — encryption failed"
    )
    assert "TELEGRAM_BOT_TOKEN=ENC[" in content, (
        "encrypted KEY=ENC[...] format not found — check sops invocation"
    )


@pytest.mark.skipif(
    not (_has_sops() and _has_age_key()),
    reason="needs sops + age key for encryption step",
)
def test_script_refuses_to_clobber_without_force(tmp_path: Path):
    """Test 7: pre-create the dir, run without --force, expect exit 2."""
    data_repo = _make_tmp_data_repo(tmp_path)
    tenant_dir = data_repo / "tenants" / "test-acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "marker.txt").write_text("pre-existing")

    result = _run(["test-acme"], data_repo=data_repo)
    assert result.returncode == 2, (
        f"expected exit 2 (clobber refusal), got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    msg = (result.stderr + result.stdout).lower()
    assert "already exists" in msg
    assert "--force" in msg

    # Marker file should still be there (no destructive write).
    assert (tenant_dir / "marker.txt").is_file()
    assert (tenant_dir / "marker.txt").read_text() == "pre-existing"


@pytest.mark.skipif(
    not (_has_sops() and _has_age_key()),
    reason="needs sops + age key for encryption step",
)
def test_script_overwrites_with_force(tmp_path: Path):
    """Test 8: pre-create with marker, run with --force, expect success + marker gone."""
    data_repo = _make_tmp_data_repo(tmp_path)
    tenant_dir = data_repo / "tenants" / "test-acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "marker.txt").write_text("pre-existing")

    result = _run(["test-acme", "--force"], data_repo=data_repo)
    assert result.returncode == 0, (
        f"expected exit 0 with --force, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    # Marker should be gone (force wiped the dir clean).
    assert not (tenant_dir / "marker.txt").exists(), (
        "marker.txt should be wiped by --force"
    )
    # Expected scaffolding files should be there.
    assert (tenant_dir / "tenant.yaml").is_file()
    assert (tenant_dir / "secrets.sops.env").is_file()
    assert (tenant_dir / "README.md").is_file()


def test_script_fails_loudly_when_sops_yaml_missing(tmp_path: Path):
    """Test 9 (regression 2026-05-09): when DATA_REPO has no .sops.yaml, the
    script must exit-fail with a clear error AND leave NO plaintext file
    behind.

    Bug history: subagent's first version of the script let `sops --encrypt`
    fail silently (subshell exit code didn't propagate through `set -e` in
    a compound statement), leaving a PLAINTEXT secrets.sops.env on disk.
    Operator could git-add it without realizing. Fix: explicit exit-code
    capture + grep for ENC[ markers + cleanup of plaintext on failure.
    """
    # Build a tmp data repo WITHOUT copying .sops.yaml — simulates a fresh
    # repo someone just `mkdir`'d.
    data_repo = tmp_path / "bubble-vps-data"
    (data_repo / "tenants").mkdir(parents=True)
    # NOTE: deliberately NOT copying SOPS_YAML_REAL into data_repo

    result = _run(["test-acme"], data_repo=data_repo)
    assert result.returncode != 0, (
        f"expected non-zero exit, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    # Error must mention .sops.yaml so the operator knows what to fix.
    msg = (result.stderr + result.stdout).lower()
    assert ".sops.yaml" in msg, (
        f"error message must mention .sops.yaml. Got:\n{msg}"
    )

    # CRITICAL: secrets.sops.env must NOT exist as plaintext on disk.
    secrets_file = data_repo / "tenants" / "test-acme" / "secrets.sops.env"
    if secrets_file.exists():
        # Read as plaintext; if it has the placeholder strings unencrypted,
        # the bug is not fully fixed.
        content = secrets_file.read_text()
        assert "PASTE_FROM_BOTFATHER" not in content, (
            f"plaintext placeholders found in {secrets_file} after sops failure — "
            f"the bug from 2026-05-09 has regressed!"
        )
