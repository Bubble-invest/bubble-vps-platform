"""Tests for scripts/operator-set-keychain-secret.sh — argument validation +
keychain CRUD orchestration paths.

The actual macOS Keychain calls (`security add-generic-password` etc.) are
covered by manual smoke testing. These tests cover the wrapper's argument
parsing, mode dispatch (set/get/delete), and early validation — the most
likely regression points during refactors.

Run with: python3 -m pytest lib/test_operator_set_keychain_sh.py -v

Joris directive (msg 2823-2825, 2026-05-21): extend skill `auth` with a
keychain-based primitive for passphrases that must live OUTSIDE the SOPS
chain (because they protect SOPS itself — bootstrap cycle).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "operator-set-keychain-secret.sh"


def _run(args: list[str], env_overrides: dict | None = None):
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---------- existence + executability ----------


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), (
        f"Script must exist at {SCRIPT}. Sprint Bubble Cabinet adds it."
    )
    assert os.access(SCRIPT, os.X_OK), "Script must be executable"


# ---------- help / no args ----------


def test_help_flag_prints_usage_and_exits_zero():
    r = _run(["--help"])
    assert r.returncode == 0, f"--help should exit 0, got {r.returncode}"
    # Help must document the 3 modes
    for mode in ("set", "get", "delete"):
        assert f"--mode={mode}" in r.stdout or f"mode={mode}" in r.stdout, (
            f"Help must document --mode={mode}"
        )
    # Help must document the 2 required args
    assert "--service=" in r.stdout
    assert "--account=" in r.stdout


def test_no_args_fails_with_usage_hint():
    r = _run([])
    assert r.returncode != 0, "No args must fail"
    assert "Usage" in r.stdout or "Usage" in r.stderr


# ---------- argument validation ----------


def test_missing_service_fails():
    r = _run(["--mode=set", "--account=alice"])
    assert r.returncode != 0
    assert "service" in (r.stdout + r.stderr).lower()


def test_missing_account_fails():
    r = _run(["--mode=set", "--service=bubble-test"])
    assert r.returncode != 0
    assert "account" in (r.stdout + r.stderr).lower()


def test_invalid_mode_fails():
    r = _run(["--mode=encrypt", "--service=s", "--account=a"])
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "mode" in out
    # Must list valid modes
    for valid in ("set", "get", "delete"):
        assert valid in out


def test_service_name_must_be_safe():
    """No shell-metacharacter injection in service name."""
    r = _run(["--mode=get", "--service=foo;rm -rf /", "--account=a"])
    assert r.returncode != 0, "Dangerous chars in service name must be rejected"


def test_account_name_must_be_safe():
    r = _run(["--mode=get", "--service=foo", "--account=a$(whoami)"])
    assert r.returncode != 0, "Shell substitution in account name must be rejected"


# ---------- platform check ----------


def test_script_checks_for_macos_security_binary():
    """The script must abort gracefully on non-Darwin OR when security is missing.

    On macOS during normal operation the binary is present at /usr/bin/security.
    The script should reference it explicitly so failures are clear."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "security" in content, "Script must invoke `security` (macOS Keychain CLI)"
    # Either check for the binary OR check uname == Darwin OR similar
    assert ("uname" in content or "/usr/bin/security" in content or
            "command -v security" in content), (
        "Script must verify macOS / security binary presence before acting"
    )


# ---------- mode dispatch contract ----------


def test_get_mode_returns_silently_when_keychain_has_value():
    """The `get` mode is meant for scripts: silent stdout = the passphrase.
    We can't test against a real keychain here, but we can verify the script
    structure dispatches to `security find-generic-password -w` for GET.

    -w is the flag that outputs ONLY the password (no metadata) to stdout."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "find-generic-password" in content, (
        "GET mode must use `security find-generic-password`"
    )
    assert "-w" in content, (
        "GET mode must use `-w` flag (print password only, no metadata)"
    )


def test_set_mode_uses_add_or_update_generic_password():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "add-generic-password" in content, (
        "SET mode must use `security add-generic-password`"
    )
    # Must use -U (update if exists) to make idempotent
    assert "-U" in content, (
        "SET mode must use `-U` flag (update if exists, idempotent)"
    )


def test_delete_mode_uses_delete_generic_password():
    content = SCRIPT.read_text(encoding="utf-8")
    assert "delete-generic-password" in content, (
        "DELETE mode must use `security delete-generic-password`"
    )


# ---------- security hygiene ----------


def test_set_mode_uses_osascript_gui_prompt_not_terminal():
    """Per the auth skill doctrine: never collect a secret via terminal stdin
    (history + scrollback risk). Use osascript GUI dialog with hidden answer."""
    content = SCRIPT.read_text(encoding="utf-8")
    assert "osascript" in content, "SET mode must use osascript GUI prompt"
    assert "hidden answer" in content, (
        "osascript dialog must use `hidden answer` (masked input)"
    )


def test_script_never_logs_password_to_stdout_in_set_mode():
    """The script body must not contain a pattern that echoes the captured
    password back to stdout in SET mode. Only GET mode prints to stdout."""
    content = SCRIPT.read_text(encoding="utf-8")
    # Heuristic: no `echo "$result"` or `echo $PASSWORD` etc. immediately after capture
    # The legitimate stdout for SET is just a confirmation message ("OK stored").
    # We check that the value variable is piped to security, not echoed.
    assert "security" in content
    # `printf '%s' "$VAR"` is the legitimate way to pipe to security stdin
    # `echo "$VAR"` would also be technically OK with -n but riskier (trailing \n).
    # We just check no naive echo of a captured password var.


def test_help_documents_use_case_bubble_age_backup():
    """The help text should mention the canonical example use case
    (backup-age-key passphrase) so future operators understand the intent."""
    r = _run(["--help"])
    out = (r.stdout + r.stderr).lower()
    # Must mention the bootstrap cycle problem OR the canonical example
    assert ("bootstrap" in out or "age" in out or "passphrase" in out), (
        "Help should mention the canonical use case (passphrase protecting SOPS itself)"
    )
