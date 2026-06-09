"""Tests for scripts/provision-tenant.sh — Hetzner box provisioning bootstrapper.

Per SPEC-017 §"Test plan" — 9 tests + 1 dry-run integration test.

ALL tests are STATIC (we don't actually provision a Hetzner box in CI — costs
real money + ~5 min wait + manual cleanup). Tests assert script source patterns,
arg-rejection behavior, and a dry-run path that uses a fake hcloud binary on
PATH (asserts no hcloud server create is called when --dry-run is passed).

Run with: python3 -m pytest lib/test_provision_tenant_script.py -v
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "provision-tenant.sh"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _read_script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _make_tmp_data_repo_with_tenant(tmp_path: Path, tenant: str = "test-prov") -> Path:
    """Create a tmp_path-based data repo with a minimal tenant.yaml in place.

    For dry-run / arg-rejection tests we don't need real SOPS / age — we just
    need the file to exist so the script's existence check passes.
    """
    data_repo = tmp_path / "bubble-vps-data"
    tenant_dir = data_repo / "tenants" / tenant
    tenant_dir.mkdir(parents=True)
    # Minimal tenant.yaml — the dry-run path doesn't validate it, just checks it exists.
    (tenant_dir / "tenant.yaml").write_text(
        f"tenant_name: {tenant}\n"
        "tenant_type: client\n"
        "host:\n"
        "  ip: PLACEHOLDER_FILL_AFTER_HETZNER_PROVISION\n"
    )
    return data_repo


def _run(args: list[str], data_repo: Path | None = None,
         extra_env: dict[str, str] | None = None,
         path_prepend: str | None = None,
         timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if data_repo is not None:
        env["BUBBLE_DATA_REPO"] = str(data_repo)
    if extra_env:
        env.update(extra_env)
    if path_prepend:
        env["PATH"] = f"{path_prepend}:{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _make_fake_bin_dir(tmp_path: Path,
                       hcloud_behavior: str = "list-only-allowed") -> Path:
    """Create a tmp dir with a fake hcloud + security binary.

    `hcloud_behavior`:
      - "list-only-allowed": list/describe commands return canned output;
        ANY `server create` call exits 99 with an error message (so we can
        assert the script does NOT invoke it during --dry-run).

    The fake `security` binary returns a fake-but-non-empty token so the
    Keychain check passes without touching the real Keychain.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    # Fake hcloud — handles list/describe + refuses server create.
    fake_hcloud = fake_bin / "hcloud"
    fake_hcloud.write_text("""#!/usr/bin/env bash
# Fake hcloud for tests. Records all calls to /tmp/.../hcloud-calls.log
# (path comes from FAKE_HCLOUD_LOG env var).
if [[ -n "${FAKE_HCLOUD_LOG:-}" ]]; then
    echo "$@" >> "$FAKE_HCLOUD_LOG"
fi

case "$1 $2" in
    "ssh-key list")
        echo "12345 joris-fake-key"
        exit 0
        ;;
    "firewall list")
        echo "10938002 bubble-default"
        exit 0
        ;;
    "server list")
        # No existing server — empty output is fine.
        exit 0
        ;;
    "server describe")
        # Should not be called in dry-run mode.
        echo '{"id": 999, "public_net": {"ipv4": {"ip": "203.0.113.1"}}}'
        exit 0
        ;;
    "server create")
        # CRITICAL: this MUST NOT be called during --dry-run.
        echo "FAKE-HCLOUD: server create was called! args: $*" >&2
        exit 99
        ;;
    *)
        # Allow other commands through silently.
        exit 0
        ;;
esac
""")
    fake_hcloud.chmod(0o755)

    # Fake security — returns a fake non-empty token.
    fake_security = fake_bin / "security"
    fake_security.write_text("""#!/usr/bin/env bash
# Fake `security` for tests — returns a fake HCLOUD token.
if [[ "$1" == "find-generic-password" ]]; then
    # Fake token (64 hex chars, looks plausible).
    echo "0000000000000000000000000000000000000000000000000000000000000000"
    exit 0
fi
exit 1
""")
    fake_security.chmod(0o755)

    return fake_bin


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_script_exists_and_executable():
    """Test 1: file exists, mode includes user-executable bit."""
    assert SCRIPT.is_file(), f"script not found at {SCRIPT}"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"script not user-executable: {SCRIPT}"


def test_script_rejects_no_args(tmp_path: Path):
    """Test 2: no positional arg → exit 2 with clear message."""
    data_repo = _make_tmp_data_repo_with_tenant(tmp_path)
    result = _run([], data_repo=data_repo)
    assert result.returncode == 2, (
        f"expected exit 2 (usage error), got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    msg = (result.stderr + result.stdout).lower()
    assert "tenant-name" in msg or "usage" in msg


def test_script_rejects_missing_tenant(tmp_path: Path):
    """Test 3: tenant-name with no scaffolding → exit-fail."""
    data_repo = _make_tmp_data_repo_with_tenant(tmp_path, tenant="exists")
    # Pass a name that does NOT have a scaffolding dir.
    result = _run(["does-not-exist"], data_repo=data_repo)
    assert result.returncode != 0, (
        f"expected non-zero exit, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    msg = (result.stderr + result.stdout).lower()
    assert "scaffolding" in msg or "tenant.yaml" in msg or "not found" in msg


def test_script_uses_hcloud_token_from_keychain():
    """Test 4: script source contains the keychain extraction line."""
    src = _read_script()
    # Per SPEC-017: must use exact service name "hetzner-cloud" + account "api_token".
    assert 'security find-generic-password' in src, (
        "script must use `security find-generic-password` to read keychain"
    )
    assert '-s "hetzner-cloud"' in src or "-s 'hetzner-cloud'" in src, (
        "script must reference service name 'hetzner-cloud'"
    )
    assert "-a api_token" in src, "script must reference account 'api_token'"
    assert "-w" in src, "script must use -w flag to print password to stdout"


def test_script_attaches_firewall():
    """Test 5: script source includes --firewall flag in hcloud server create."""
    src = _read_script()
    assert "--firewall" in src, "script must pass --firewall to hcloud server create"
    # Verify the firewall ID is captured from `hcloud firewall list`.
    assert "hcloud firewall list" in src, (
        "script must look up firewall ID via `hcloud firewall list`"
    )
    assert "bubble-default" in src, (
        "script must reference the 'bubble-default' firewall by name"
    )


def test_script_attaches_ssh_key():
    """Test 6: script source includes --ssh-key flag in hcloud server create."""
    src = _read_script()
    assert "--ssh-key" in src, "script must pass --ssh-key to hcloud server create"
    assert "hcloud ssh-key list" in src, (
        "script must look up SSH key ID via `hcloud ssh-key list`"
    )


def test_script_waits_for_ssh():
    """Test 7: script has the until-loop / for-loop pattern with ssh + 'echo ready'."""
    src = _read_script()
    # Either a `for` loop or `until` loop with ssh + echo reachability check.
    has_loop = ("for attempt in" in src) or ("until ssh" in src)
    assert has_loop, "script must have a polling loop waiting for SSH reachability"
    # The reachability check must use `ssh ... 'echo ready'` (or similar).
    assert "echo ready" in src, (
        "script must use `echo ready` as the SSH reachability sentinel"
    )
    # And there must be a sleep so it's not a tight loop.
    assert "sleep" in src, "script must sleep between SSH retries (not a tight loop)"


def test_script_updates_tenant_yaml_after_provision():
    """Test 8: script has python yaml-edit block + uses env-var-prefix form
    (regression rule from Step 7a — env vars BEFORE python3, not after)."""
    src = _read_script()
    # Must invoke python3 with a heredoc to do the YAML edit.
    assert "python3 <<" in src, "script must use python3 + heredoc for yaml-edit"
    # Must import yaml + write back.
    assert "import yaml" in src, "yaml-edit block must import yaml"
    assert "yaml.safe_dump" in src or "yaml.dump" in src, (
        "yaml-edit block must serialize back via yaml.safe_dump"
    )
    # Must touch host.ip + host.hostname + provider_server_id.
    assert "host" in src, "yaml-edit block must update the host section"
    assert "ip" in src and "hostname" in src and "provider_server_id" in src, (
        "yaml-edit block must update host.ip + hostname + provider_server_id"
    )

    # CRITICAL regression rule (Step 7a): env-var-prefix form must come BEFORE
    # python3, not after. We check the source for the pattern:
    #   __SOMETHING__="$VAR" \
    #   ... \
    #   python3 <<...
    # i.e. lines with `__VAR__=` should APPEAR ABOVE the `python3 <<` line.
    lines = src.splitlines()
    yaml_block_starts = [i for i, ln in enumerate(lines)
                         if "python3 <<" in ln and "__SERVER_IP__" not in ln]
    # At least one python3 <<EOF block must exist.
    py_blocks = [i for i, ln in enumerate(lines) if "python3 <<" in ln]
    assert py_blocks, "no `python3 <<` heredoc found"

    # For each python3 heredoc block, ensure that the lines IMMEDIATELY ABOVE
    # contain env-var-prefix form (`__VAR__="$X" \` pattern), NOT after.
    for idx in py_blocks:
        # Look back up to 10 lines for env-var-prefix form.
        lookback = "\n".join(lines[max(0, idx - 10):idx])
        assert "__" in lookback and "=" in lookback, (
            f"python3 heredoc at line {idx + 1} has no env-var-prefix above it.\n"
            f"Lines above:\n{lookback}\n"
            f"REGRESSION: env vars must come BEFORE python3, not after."
        )
        # Negative check: there should NOT be a `__VAR__=` on the SAME line as
        # python3 placed AFTER python3, e.g. `python3 __VAR__=val`. Bash treats
        # that as positional args.
        line = lines[idx]
        # Tokens after `python3` should not contain `__`.
        post = line.split("python3", 1)[1]
        assert "__" not in post, (
            f"python3 line at {idx + 1} has env-var-prefix AFTER python3: {line}\n"
            f"REGRESSION: bash treats post-python3 VAR=val as positional arg."
        )


def test_script_does_not_print_token_to_stdout():
    """Test 9 (SPEC-008 hard rule): no `echo $HCLOUD_TOKEN` or similar leaks."""
    src = _read_script()
    forbidden_patterns = [
        "echo $HCLOUD_TOKEN",
        'echo "$HCLOUD_TOKEN"',
        "echo ${HCLOUD_TOKEN}",
        'echo "${HCLOUD_TOKEN}"',
        "printf %s $HCLOUD_TOKEN",
        'printf "%s" "$HCLOUD_TOKEN"',
        "printf '%s' \"$HCLOUD_TOKEN\"",
        "cat <<< $HCLOUD_TOKEN",
        "echo $HCLOUD_TOKEN >",
    ]
    for pat in forbidden_patterns:
        assert pat not in src, (
            f"SPEC-008 VIOLATION: script contains token-leak pattern: {pat!r}"
        )

    # Stronger check: HCLOUD_TOKEN must only appear on the "read from keychain"
    # line (where it's CAPTURED), the export line, the cleanup unset line, and
    # comments. It should NOT appear on any echo/printf/log line.
    for line_num, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # comments are fine
        if "HCLOUD_TOKEN" not in line:
            continue
        # Allowed contexts (substring match within the non-comment line):
        allowed = (
            "HCLOUD_TOKEN=$(security " in line  # capture from keychain
            or "export HCLOUD_TOKEN" in line   # export to env
            or "unset HCLOUD_TOKEN" in line    # cleanup
            or 'z "$HCLOUD_TOKEN"' in line     # zero-length check
            or "[[ -z" in line                  # zero-length check (broader)
        )
        # Also allow naked references within trap / function bodies that
        # only `unset` it.
        if not allowed:
            # echo / printf / log are forbidden.
            for forbidden_cmd in ("echo", "printf", "logger", "tee", ">>"):
                if forbidden_cmd in line:
                    raise AssertionError(
                        f"SPEC-008 VIOLATION at line {line_num}: "
                        f"HCLOUD_TOKEN appears in suspicious context: {line!r}"
                    )


def test_script_dry_run_does_not_call_hcloud_server_create(tmp_path: Path):
    """Test 10: --dry-run with a fake hcloud + security on PATH that fails on
    `server create`. Script must NOT call server create, and exit 0.

    This is the ONLY semi-integration test; everything else is static source-
    inspection. We use a fake hcloud binary that records all invocations to a
    log file and exits non-zero if `server create` is ever invoked.
    """
    data_repo = _make_tmp_data_repo_with_tenant(tmp_path, tenant="test-prov-dry")
    fake_bin = _make_fake_bin_dir(tmp_path)
    log_file = tmp_path / "hcloud-calls.log"

    result = _run(
        ["test-prov-dry", "--dry-run"],
        data_repo=data_repo,
        extra_env={"FAKE_HCLOUD_LOG": str(log_file)},
        path_prepend=str(fake_bin),
        timeout=30,
    )

    assert result.returncode == 0, (
        f"expected exit 0 in --dry-run, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    # The log file should exist (since we called hcloud at least for ssh-key /
    # firewall list during the discovery phase).
    assert log_file.is_file(), "fake hcloud should have been invoked at least once"

    # CRITICAL: NO `server create` call should appear in the log.
    log_content = log_file.read_text()
    assert "server create" not in log_content, (
        f"--dry-run mode invoked `hcloud server create`! Log:\n{log_content}"
    )

    # Sanity: the script printed all the inputs in dry-run mode.
    out = result.stdout
    assert "test-prov-dry-vps" in out, "dry-run should print the server name"
    assert "fsn1" in out, "dry-run should print the region"
    assert "cx33" in out, "dry-run should print the type"
    assert "ubuntu-24.04" in out, "dry-run should print the image"
    assert "bubble-default" in out, "dry-run should print the firewall"
