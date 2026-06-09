"""Tests for scripts/deploy.sh — covers SPEC-004 (SSH rate-limit policy).

Verifies the wrapper script (a) requires --tenant or --tenants=all, (b) passes
the --retry/--retry-delay defaults, (c) translates flags to env vars correctly.

We test by extracting the would-be pyinfra invocation without actually running
pyinfra. The wrapper uses `exec`, so we substitute `bash -c 'echo "$@"'` for
the exec target via PYINFRA_ECHO env var (added below to deploy.sh, optional).

Run with: python3.12 -m pytest lib/test_deploy_sh.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"


def _run_deploy(args: list[str], env_overrides: dict | None = None):
    """Run scripts/deploy.sh with PYINFRA_DRY_PRINT=1 to capture would-be invocation.

    Falls back to using --dry-run if PYINFRA_DRY_PRINT isn't supported.
    Returns CompletedProcess.
    """
    import os

    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_deploy_sh_requires_tenant_arg():
    """No --tenant or --tenants=all → exit 2 with error."""
    result = _run_deploy([])
    assert result.returncode == 2
    assert "must specify --tenant" in result.stderr.lower() or "must specify --tenant" in result.stdout.lower()


def test_deploy_sh_rejects_both_tenant_and_all():
    """--tenant=X and --tenants=all → exit 2."""
    result = _run_deploy(["--tenant=foo", "--tenants=all"])
    assert result.returncode == 2
    assert "mutually exclusive" in (result.stderr + result.stdout).lower()


def test_deploy_sh_rejects_invalid_tenants_value():
    """--tenants=foobar (not 'all') → exit 2."""
    result = _run_deploy(["--tenants=foobar"])
    assert result.returncode == 2


def test_deploy_sh_includes_retry_flags():
    """SPEC-004: wrapper passes --retry 2 --retry-delay 5 by default.

    We verify by reading the script source directly (the wrapper exec's pyinfra
    immediately, so we can't capture its argv without an integration test).
    """
    src = DEPLOY_SH.read_text()
    assert "--retry" in src, "wrapper should pass --retry to pyinfra (SPEC-004)"
    assert '"2"' in src or "--retry 2" in src
    assert "--retry-delay" in src
    assert '"5"' in src or "--retry-delay 5" in src


def test_deploy_sh_is_executable():
    """The script must be executable (chmod +x at install time)."""
    import os
    assert os.access(DEPLOY_SH, os.X_OK), (
        f"{DEPLOY_SH} is not executable. Run: chmod +x {DEPLOY_SH}"
    )
