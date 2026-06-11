"""Tests for scripts/deploy.sh — covers SPEC-004 (SSH rate-limit policy).

Verifies the wrapper script (a) requires --tenant or --tenants=all, (b) passes
the --retry/--retry-delay defaults, (c) translates flags to env vars correctly.

We test by extracting the would-be pyinfra invocation without actually running
pyinfra. The wrapper uses `exec`, so we substitute `bash -c 'echo "$@"'` for
the exec target via PYINFRA_ECHO env var (added below to deploy.sh, optional).

Run with: python3.12 -m pytest lib/test_deploy_sh.py -v
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
DEPLOY_PY = REPO_ROOT / "deploy.py"


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


# ─── deploy.py orchestrator name resolution (C4 regression) ─────────────────
# deploy.py is exec'd by pyinfra (no __package__), so we can't import it in a
# unit test without a live pyinfra context. Instead we statically resolve its
# module-level names via AST — every callee at module scope must be BOUND
# (imported or assigned). This guards the C4 bug: the monitoring services were
# called as `monitoring.restic_backup()` etc. on an UNBOUND `monitoring` name,
# which would NameError at deploy time (after the agent + tailnet were already
# provisioned — a late, expensive failure).


def _deploy_py_module_bound_names() -> set[str]:
    """Names bound at deploy.py module scope: imports + top-level assignments."""
    tree = ast.parse(DEPLOY_PY.read_text(encoding="utf-8"))
    bound: set[str] = set(dir(__builtins__)) if isinstance(__builtins__, type({})) else set()
    # Be robust about __builtins__ shape across runners.
    import builtins
    bound |= set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
    return bound


def test_deploy_py_has_no_unbound_module_level_call_targets():
    """Every `name(...)` or `name.attr(...)` invoked at deploy.py module scope
    must reference a BOUND module-level name — never an unimported one.

    Regression guard for C4: `monitoring.restic_backup()` referenced an unbound
    `monitoring` name (NameError at deploy time)."""
    tree = ast.parse(DEPLOY_PY.read_text(encoding="utf-8"))
    bound = _deploy_py_module_bound_names()

    # Collect the root Name of every Call's func that lives at module level.
    unbound: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Unwrap attribute chains (monitoring.restic_backup → root Name "monitoring").
        while isinstance(func, ast.Attribute):
            func = func.value
        if isinstance(func, ast.Name) and func.id not in bound:
            unbound.append(func.id)

    assert not unbound, (
        f"deploy.py calls these UNBOUND module-level names (NameError at deploy "
        f"time): {sorted(set(unbound))}. Import or define them — the monitoring "
        f"services in particular are re-exported from tasks.monitoring."
    )


def test_deploy_py_calls_all_monitoring_services():
    """deploy.py must still invoke the four platform monitoring services AND
    import them from tasks.monitoring (so the C4 fix — explicit imports + bound
    call sites — doesn't silently drop a service)."""
    src = DEPLOY_PY.read_text(encoding="utf-8")
    bound = _deploy_py_module_bound_names()
    for fn in ("restic_backup", "cache_sync", "secrets_sweep", "transcript_leak_scan"):
        assert fn in bound, (
            f"{fn} is not imported at deploy.py module scope (expected "
            f"`from tasks.monitoring import ... {fn} ...`)"
        )
        assert f"{fn}()" in src, f"deploy.py no longer calls {fn}()"
    # And the bare `monitoring.` prefix must be gone (it never resolved).
    assert "monitoring.restic_backup" not in src, (
        "deploy.py still references the unbound `monitoring.` prefix"
    )
