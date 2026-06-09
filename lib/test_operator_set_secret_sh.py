"""Tests for scripts/operator-set-secret.sh — argument validation paths.

The actual GUI prompt + sops integration is covered by manual smoke testing
(it requires user input or a real secrets file). These tests cover the
wrapper's argument parsing + early validation, which are the most likely
regression points during refactors.

Run with: python3.12 -m pytest lib/test_operator_set_secret_sh.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "operator-set-secret.sh"


def _run(args: list[str], env_overrides: dict | None = None):
    import os

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


def test_script_exists_and_is_executable():
    import os
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


def test_missing_tenant_arg_fails():
    # After generalizing to --project mode, no-mode is signalled with a
    # different message. The invariant: exit 2 + some guidance about --tenant
    # or --project being required.
    result = _run(["--key=FOO"])
    assert result.returncode == 2
    msg = (result.stderr + result.stdout).lower()
    assert "tenant" in msg or "project" in msg, (
        f"expected error mentioning --tenant or --project, got: {result.stderr!r}"
    )


def test_missing_key_arg_fails():
    result = _run(["--tenant=bubble-internal"])
    assert result.returncode == 2
    assert "missing --key" in result.stderr.lower() or "missing --key" in result.stdout.lower()


def test_invalid_key_format_fails():
    result = _run(["--tenant=bubble-internal", "--key=lower_case"])
    assert result.returncode == 2
    msg = (result.stderr + result.stdout).lower()
    assert "upper_snake_case" in msg or "must be upper" in msg


def test_unknown_arg_fails():
    result = _run(["--bogus=foo"])
    assert result.returncode == 2


def test_nonexistent_tenant_fails():
    # After generalization the per-mode path check uses a shared "directory not
    # found" message (no longer "tenant directory not found"). Invariant: exit 1
    # + "not found" in output.
    result = _run(["--tenant=does-not-exist-xyz", "--key=FOO_BAR"])
    assert result.returncode == 1
    assert "not found" in (result.stderr + result.stdout).lower(), (
        f"expected 'not found' in error output, got: {result.stderr!r}"
    )


def test_help_prints_usage():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "usage" in result.stdout.lower() or "tenant=" in result.stdout


def test_script_changes_cwd_before_sops_encrypt():
    """Regression test for sops cwd-based .sops.yaml discovery bug.

    sops walks UP from the CURRENT WORKING DIRECTORY (not from the file path)
    to find .sops.yaml for `--encrypt --in-place`. The script was originally
    invoked from the platform repo dir and sops couldn't find the data repo's
    .sops.yaml. Caught a real bug on 2026-05-08 — {{OPERATOR}}'s second paste failed
    after the env-var-prefix fix because cwd was wrong.

    After generalization (2026-05-12), the script uses a unified $SOPS_CWD
    variable (set to $DATA_REPO in tenant mode, $PROJECT_DIR in project mode).
    The script must `cd "$SOPS_CWD"` before any executable `sops --encrypt`
    invocation (NOT counting comment occurrences).
    """
    import re

    src = SCRIPT.read_text()
    # Strip comments to look only at executable lines
    code_lines = [
        line for line in src.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)

    # The cwd change is now `cd "$SOPS_CWD"` (generalized from `cd "$DATA_REPO"`)
    cd_idx = code.find('cd "$SOPS_CWD"')
    # Find the FIRST executable `sops --encrypt` (excludes comments by virtue
    # of the strip above). Use a regex to ensure we match the command form.
    encrypt_match = re.search(r'\bsops\s+--encrypt\b', code)
    assert cd_idx > 0, (
        "missing 'cd \"$SOPS_CWD\"' — regression guard for sops cwd discovery "
        "(originally 'cd \"$DATA_REPO\"', generalized 2026-05-12)"
    )
    assert encrypt_match is not None, "expected sops --encrypt invocation not found"
    assert cd_idx < encrypt_match.start(), (
        f"cd $SOPS_CWD (idx {cd_idx}) must come BEFORE sops --encrypt "
        f"(idx {encrypt_match.start()}) — see {{OPERATOR}} regression 2026-05-08"
    )


def test_python_envvar_pass_through_correctly_prefixed():
    """Regression test for env-var pass-through bug.

    bash treats `VAR=val cmd` differently depending on whether VAR=val comes
    BEFORE or AFTER `cmd`. Before = sets VAR for the subprocess. After = passes
    as a positional arg. The script's python heredoc relies on env vars, so
    the env-var-prefix form MUST appear before `python3`. Caught a real bug
    on 2026-05-08 — {{OPERATOR}}'s first paste failed because the env vars came after.
    """
    src = SCRIPT.read_text()
    # The env-var prefix should come BEFORE python3:
    # __VAR__="$VAL" python3 -c "..."
    # NOT:
    # python3 -c "..." __VAR__="$VAL"
    import re
    # find every python3 -c invocation and check ANY __DUNDER__= prefix is
    # within ~200 chars BEFORE it (i.e. env-var-prefix form, not positional)
    for match in re.finditer(r'python3\s+-c\s+"', src):
        before = src[:match.start()].rstrip()
        last_200_before = before[-200:]
        # At least one __DUNDER_VAR__= assignment must precede python3
        assert re.search(r'__[A-Z_]+__\s*=', last_200_before), (
            f"env-var-prefix form must precede python3 invocation at offset "
            f"{match.start()}; see {{OPERATOR}} regressions 2026-05-08"
        )


def test_value_sanitization_strips_whitespace_and_newlines():
    """Regression test for the dotenv-newline bug.

    A value pasted via osascript dialog can contain a trailing newline (or
    embedded CR) that makes the dotenv parser choke with "invalid dotenv input
    line: <fragment>" — and the fragment leaks into stderr → transcript.
    Sanitization must strip whitespace and remove embedded \\n / \\r before
    writing to the dotenv file. Caught a real bug 2026-05-08.
    """
    src = SCRIPT.read_text()
    # The sanitization block uses python3 with __VAL__ env var. It must:
    # 1. Read os.environ['__VAL__']
    # 2. .strip() the value
    # 3. .replace('\n', '') and .replace('\r', '')
    sanitization_block = src
    assert "os.environ['__VAL__']" in sanitization_block, (
        "missing value sanitization block — regression 2026-05-08"
    )
    assert "v.strip()" in sanitization_block or ".strip()" in sanitization_block
    assert "replace('\\n'" in sanitization_block or 'replace("\\n"' in sanitization_block, (
        "value sanitization must strip embedded newlines"
    )
    assert "replace('\\r'" in sanitization_block or 'replace("\\r"' in sanitization_block, (
        "value sanitization must strip embedded carriage returns"
    )


def test_sops_stderr_captured_to_tmpfile_not_stdout():
    """Regression test for sops-stderr-fragment-leak bug.

    sops's dotenv parser, on error, prints "invalid dotenv input line: <fragment>"
    to stderr — where <fragment> is an actual piece of the value. If sops's
    stderr is left attached to the script's stderr (default), that fragment
    lands in operator transcripts. Mitigation: capture sops stderr to a tmp
    file (NOT printed by the script). Caught a real bug 2026-05-08.
    """
    src = SCRIPT.read_text()
    # Strip comments (lines starting with #, after optional whitespace) to
    # avoid false-matching backtick-quoted command names in comments.
    code_lines = [
        line for line in src.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)

    # sops --decrypt and sops --encrypt invocations in EXECUTABLE code must
    # redirect stderr to a tmp file (look for 2>"$SOPS_STDERR" pattern).
    import re
    sops_calls = re.findall(r'sops\s+--(?:decrypt|encrypt)[^\n]*', code)
    # Filter out --version verifications (no values touched there).
    sops_calls = [c for c in sops_calls if "--version" not in c]
    assert len(sops_calls) >= 2, (
        f"expected at least decrypt + encrypt sops calls, got: {sops_calls}"
    )
    import re as _re
    # Accept any 2>"$SOPS_STDERR*" pattern — the script may use multiple
    # tmp-file vars (e.g. $SOPS_STDERR for main, $SOPS_STDERR_VERIFY for
    # post-update sanity check). The key invariant is that stderr is captured
    # to A tmp file, not let it bubble to script stderr → transcript.
    stderr_redirect_re = _re.compile(r'2>\s*"\$SOPS_STDERR[A-Z_]*"')
    for call in sops_calls:
        assert stderr_redirect_re.search(call), (
            f"sops invocation must redirect stderr to a $SOPS_STDERR* tmp file "
            f"(regression 2026-05-08): {call!r}"
        )


# ─── Project mode tests (added 2026-05-12) ───────────────────────────────────

def test_project_mode_mutual_exclusion():
    """--tenant and --project are mutually exclusive."""
    result = _run(["--tenant=foo", "--project=/tmp", "--key=BAR"])
    assert result.returncode == 2
    assert "mutually exclusive" in (result.stderr + result.stdout).lower()


def test_project_mode_nonexistent_path_rejected():
    """--project pointing at a non-existent directory is rejected with exit 1."""
    result = _run(["--project=/nonexistent-12345-xyzzy", "--key=FOO_BAR"])
    assert result.returncode == 1
    assert "not found" in (result.stderr + result.stdout).lower()


def test_project_mode_help_documents_project_flag():
    """--help output must document the --project flag."""
    result = _run(["--help"])
    assert result.returncode == 0
    assert "project" in (result.stdout + result.stderr).lower()


def test_project_mode_missing_secrets_file_rejected():
    """--project with an existing dir but no secrets.sops.env gives exit 1."""
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run(["--project=" + tmpdir, "--key=FOO_BAR"])
        assert result.returncode == 1
        assert "not found" in (result.stderr + result.stdout).lower()


def test_project_mode_no_tenant_in_args_ok():
    """--project without --tenant does NOT emit the 'missing --tenant' error."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # No secrets file so it will fail, but NOT with the missing-mode error
        result = _run(["--project=" + tmpdir, "--key=FOO_BAR"])
        msg = (result.stderr + result.stdout).lower()
        assert "specify either" not in msg, (
            "should not hit the no-mode-specified guard when --project is given"
        )
