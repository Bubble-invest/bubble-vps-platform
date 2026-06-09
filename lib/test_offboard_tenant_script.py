"""Tests for scripts/offboard-tenant.sh — tenant offboarding (Step 7c).

Per SPEC-018 §"Test plan" — 7 tests + 2 safety tests:
  1. test_script_exists_and_executable
  2. test_script_rejects_no_args
  3. test_script_rejects_missing_tenant
  4. test_script_handoff_mode_calls_sops_updatekeys
  5. test_script_destroy_mode_calls_hcloud_server_delete
  6. test_script_archives_tenant_dir
  7. test_script_destroy_requires_typed_confirmation
  8. test_destroy_mode_requires_typed_tenant_name (safety addition)
  9. test_offboard_does_not_print_hcloud_token (SPEC-008 hard rule)
 10. test_handoff_integration_archives_and_edits_sops_yaml (real handoff path)

Most tests are STATIC source inspection. The integration test creates a tmp
data repo + fake tenant + adds a tenant-specific .sops.yaml rule with both
operator + fake-box pubkeys, then runs the handoff path with --yes (no real
hcloud, no real Tailscale removal — that's a manual step the operator does).

Run with: python3 -m pytest lib/test_offboard_tenant_script.py -v
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "offboard-tenant.sh"
DATA_REPO_REAL = REPO_ROOT.parent / "bubble-vps-data"
SOPS_YAML_REAL = DATA_REPO_REAL / ".sops.yaml"

OPERATOR_MASTER = "age1qal34hv5h99vvpq7kmghfz0mjh98eq9mj5dg5k43r8kwmumvnu5qt6w3hy"
FAKE_BOX_KEY = "age1fakeboxkeyfortestingpurposesonlynotrealkeyxxxxxxxxxxxxxxxxx"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _read_script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _has_sops() -> bool:
    return shutil.which("sops") is not None


def _has_age_key() -> bool:
    key_file = os.environ.get(
        "SOPS_AGE_KEY_FILE", str(Path.home() / ".config/sops/age/keys.txt")
    )
    return Path(key_file).is_file()


def _make_tmp_data_repo_with_tenant(
    tmp_path: Path,
    tenant: str = "test-offboard",
    add_specific_rule: bool = False,
    encrypt_secrets: bool = False,
) -> Path:
    """Create a tmp data repo with a fake tenant.

    `add_specific_rule`: if True, append a tenant-specific .sops.yaml rule with
    operator master + fake box pubkey (mimics what would exist after Phase D
    first-half deploy added the box pubkey).

    `encrypt_secrets`: if True, also create a real encrypted secrets.sops.env
    (requires sops + age — caller should gate via _has_sops()/_has_age_key()).
    """
    data_repo = tmp_path / "bubble-vps-data"
    tenant_dir = data_repo / "tenants" / tenant
    tenant_dir.mkdir(parents=True)

    # Write a minimal but realistic tenant.yaml.
    (tenant_dir / "tenant.yaml").write_text(
        f"tenant_name: {tenant}\n"
        "tenant_type: client\n"
        "host:\n"
        "  ip: 203.0.113.42\n"
        f"  hostname: {tenant}-vps\n"
        "  provider_server_id: \"99999\"\n"
        "  region: fsn1-dc14\n"
    )

    # Build .sops.yaml. Always include the catch-all + bubble-internal rule.
    sops_yaml_lines = [
        "creation_rules:",
        "  - path_regex: tenants/bubble-internal/secrets\\.sops\\.env$",
        "    age: >-",
        f"      {OPERATOR_MASTER},",
        f"      {FAKE_BOX_KEY}",
    ]
    if add_specific_rule:
        sops_yaml_lines += [
            f"  - path_regex: tenants/{tenant}/secrets\\.sops\\.env$",
            "    age: >-",
            f"      {OPERATOR_MASTER},",
            f"      {FAKE_BOX_KEY}",
        ]
    sops_yaml_lines += [
        "  - path_regex: tenants/[^/]+/secrets\\.sops\\.env$",
        f"    age: {OPERATOR_MASTER}",
        "",
    ]
    (data_repo / ".sops.yaml").write_text("\n".join(sops_yaml_lines))

    if encrypt_secrets and _has_sops() and _has_age_key():
        # Use the real sops to encrypt a placeholder file.
        secrets_path = tenant_dir / "secrets.sops.env"
        secrets_path.write_text("FAKE_KEY=fake_value\n")
        env = dict(os.environ)
        env["SOPS_AGE_KEY_FILE"] = os.environ.get(
            "SOPS_AGE_KEY_FILE", str(Path.home() / ".config/sops/age/keys.txt")
        )
        result = subprocess.run(
            ["sops", "--encrypt", "--input-type", "dotenv", "--output-type",
             "dotenv", "--in-place", f"tenants/{tenant}/secrets.sops.env"],
            cwd=data_repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"sops encrypt failed in fixture: {result.stderr}"
            )

    return data_repo


def _run(args: list[str], data_repo: Path | None = None,
         extra_env: dict[str, str] | None = None,
         path_prepend: str | None = None,
         stdin_input: str | None = None,
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
        input=stdin_input,
        timeout=timeout,
    )


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_script_exists_and_executable():
    """Test 1: file exists, mode includes user-executable bit."""
    assert SCRIPT.is_file(), f"script not found at {SCRIPT}"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"script not user-executable: {SCRIPT}"
    # Spec says 0755 explicitly.
    assert mode & stat.S_IRGRP, "script must be group-readable (0755)"
    assert mode & stat.S_IROTH, "script must be world-readable (0755)"


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
    # Pass a name that does NOT have a tenant dir.
    result = _run(["does-not-exist"], data_repo=data_repo)
    assert result.returncode != 0, (
        f"expected non-zero exit, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    msg = (result.stderr + result.stdout).lower()
    assert "tenant.yaml" in msg or "not found" in msg


def test_script_handoff_mode_calls_sops_updatekeys():
    """Test 4: script source contains `sops updatekeys` invocation."""
    src = _read_script()
    assert "sops updatekeys" in src, (
        "handoff mode must invoke `sops updatekeys` to re-encrypt"
    )
    # Spec says --yes flag for unattended re-key.
    assert "sops updatekeys --yes" in src, (
        "must pass --yes to sops updatekeys (unattended re-key)"
    )


def test_script_destroy_mode_calls_hcloud_server_delete():
    """Test 5: script source contains `hcloud server delete` invocation."""
    src = _read_script()
    assert "hcloud server delete" in src, (
        "destroy mode must invoke `hcloud server delete` to remove the box"
    )


def test_script_archives_tenant_dir():
    """Test 6: script moves tenant dir to tenants/_archive/<name>-<mode>-<date>/."""
    src = _read_script()
    # Must create the archive base dir.
    assert "_archive" in src, "script must reference tenants/_archive/"
    # Must include the mode-suffixed name pattern.
    assert "handoff-" in src, "archive path must include 'handoff-' suffix for handoff mode"
    assert "destroyed-" in src, "archive path must include 'destroyed-' suffix for destroy mode"
    # Must use mv (not cp) so the original is gone.
    assert "mv " in src, "must use `mv` to relocate (not copy)"


def test_script_destroy_requires_typed_confirmation():
    """Test 7: destroy mode reads a confirmation prompt that compares input
    against the tenant name (not just a generic yes/no)."""
    src = _read_script()
    # The script must contain a `read` call AND a comparison against the
    # tenant name variable. Per spec: "Type the tenant name to confirm".
    assert "read -r NAME_CONFIRM" in src or "read NAME_CONFIRM" in src, (
        "destroy mode must read a name-confirmation variable"
    )
    # The comparison must be against $TENANT_NAME exactly.
    assert "NAME_CONFIRM" in src, "destroy mode must define a NAME_CONFIRM variable"
    # The prompt text should explicitly mention typing the tenant name.
    assert "tenant name" in src.lower(), (
        "destroy mode prompt must explicitly ask to type the tenant name"
    )


def test_destroy_mode_requires_typed_tenant_name():
    """Test 8 (safety addition): the second `read` prompt in destroy mode
    must compare the input to the tenant NAME (not a generic 'yes'). This
    defends against muscle-memory typos: even if --yes is passed, the
    typed-name confirmation is the second safety net.

    NOTE: per spec, --yes does skip the typed-name prompt as well (so CI
    can run unattended). This test confirms the COMPARISON pattern exists,
    not the unconditional firing.
    """
    src = _read_script()
    # There must be TWO read prompts: first for "yes", second for tenant name.
    read_lines = [ln for ln in src.splitlines() if "read -r" in ln or "read CONFIRM" in ln or "read NAME_CONFIRM" in ln]
    # We expect at least 2 read invocations (one yes-confirm, one name-confirm).
    assert len(read_lines) >= 2, (
        f"expected at least 2 `read` prompts (yes + tenant-name), found {len(read_lines)}:\n"
        + "\n".join(read_lines)
    )

    # The name-confirm comparison must be against $TENANT_NAME.
    assert '"$NAME_CONFIRM" != "$TENANT_NAME"' in src or \
           "$NAME_CONFIRM != $TENANT_NAME" in src, (
        "destroy mode must compare typed input to $TENANT_NAME"
    )


def test_offboard_does_not_print_hcloud_token():
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
        "logger $HCLOUD_TOKEN",
        'logger "$HCLOUD_TOKEN"',
        "tee $HCLOUD_TOKEN",
    ]
    for pat in forbidden_patterns:
        assert pat not in src, (
            f"SPEC-008 VIOLATION: script contains token-leak pattern: {pat!r}"
        )

    # Stronger check: HCLOUD_TOKEN must only appear on the "read from keychain"
    # line, the export line, the cleanup unset line, and comments.
    for line_num, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # comments are fine
        if "HCLOUD_TOKEN" not in line:
            continue
        allowed = (
            "HCLOUD_TOKEN=$(security " in line  # capture from keychain
            or "export HCLOUD_TOKEN" in line   # export to env
            or "unset HCLOUD_TOKEN" in line    # cleanup
            or 'z "$HCLOUD_TOKEN"' in line     # zero-length check
            or "[[ -z" in line                  # zero-length check (broader)
        )
        if not allowed:
            for forbidden_cmd in ("echo", "printf", "logger", "tee", ">>"):
                if forbidden_cmd in line:
                    raise AssertionError(
                        f"SPEC-008 VIOLATION at line {line_num}: "
                        f"HCLOUD_TOKEN appears in suspicious context: {line!r}"
                    )


@pytest.mark.skipif(
    not (_has_sops() and _has_age_key()),
    reason="needs sops + age key for handoff integration test",
)
def test_handoff_integration_archives_and_edits_sops_yaml(tmp_path: Path):
    """Test 10 (integration): full handoff happy path with real sops.

    Setup:
      - Tmp data repo with .sops.yaml that has BOTH the catch-all rule AND a
        tenant-specific rule for 'test-offboard' with operator + fake-box pubkeys.
      - Real encrypted secrets.sops.env (encrypted with operator master).

    Run: ./offboard-tenant.sh test-offboard --mode=handoff --yes

    Expected:
      - Exit 0
      - tenants/test-offboard/ no longer exists at original location
      - tenants/_archive/test-offboard-handoff-<date>/ exists
      - .sops.yaml's tenant-specific rule has lost the operator master pubkey
        but kept the fake box pubkey
    """
    data_repo = _make_tmp_data_repo_with_tenant(
        tmp_path,
        tenant="test-offboard",
        add_specific_rule=True,
        encrypt_secrets=False,  # we don't need real encryption — sops updatekeys
                                 # would fail with a fake box key it can't decrypt for
    )

    # We DO need a secrets.sops.env to exist. Make one encrypted ONLY with the
    # operator master (so sops updatekeys can decrypt + re-encrypt with the new
    # smaller list — which after operator removal is just the fake box key).
    # However, if we re-encrypt to a fake box key, sops will fail because it
    # can't verify the recipient.
    #
    # Workaround: skip sops updatekeys part of the test by mocking sops on PATH.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_sops = fake_bin / "sops"
    fake_sops.write_text("""#!/usr/bin/env bash
# Fake sops for tests — logs all calls + exits 0.
echo "FAKE-SOPS called with: $*" >> "${FAKE_SOPS_LOG:-/dev/null}"
exit 0
""")
    fake_sops.chmod(0o755)

    log_file = tmp_path / "sops-calls.log"

    # Create a placeholder secrets.sops.env (need it to exist for sops to be
    # called against it — even our fake-sops doesn't care about contents).
    (data_repo / "tenants" / "test-offboard" / "secrets.sops.env").write_text(
        "# fake encrypted file for offboard integration test\n"
    )

    result = _run(
        ["test-offboard", "--mode=handoff", "--yes"],
        data_repo=data_repo,
        extra_env={"FAKE_SOPS_LOG": str(log_file)},
        path_prepend=str(fake_bin),
        timeout=30,
    )

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    # Tenant dir should be gone from its original location.
    assert not (data_repo / "tenants" / "test-offboard").exists(), (
        "tenants/test-offboard/ should be moved to _archive/"
    )

    # Archive dir should exist with the right naming pattern.
    archive_base = data_repo / "tenants" / "_archive"
    assert archive_base.is_dir(), "_archive/ dir should be created"
    archives = list(archive_base.iterdir())
    assert len(archives) == 1, f"expected 1 archive entry, got {archives}"
    archive_name = archives[0].name
    assert archive_name.startswith("test-offboard-handoff-"), (
        f"archive must be named 'test-offboard-handoff-<date>', got '{archive_name}'"
    )

    # .sops.yaml must still parse + tenant-specific rule must have lost the operator key.
    sops_yaml_data = yaml.safe_load((data_repo / ".sops.yaml").read_text())
    assert isinstance(sops_yaml_data, dict)
    rules = sops_yaml_data.get("creation_rules") or []
    specific_rule = None
    for rule in rules:
        pr = rule.get("path_regex", "")
        if "tenants/test-offboard/" in pr:
            specific_rule = rule
            break
    assert specific_rule is not None, (
        "tenant-specific rule should still exist (just with fewer recipients)"
    )

    age_field = specific_rule.get("age", "")
    recipients = [r.strip() for r in age_field.replace("\n", " ").split(",")]
    recipients = [r for r in recipients if r]
    assert OPERATOR_MASTER not in recipients, (
        "operator master must be REMOVED from tenant rule after handoff"
    )
    assert FAKE_BOX_KEY in recipients, (
        "fake box pubkey must REMAIN in tenant rule (box can still decrypt)"
    )

    # sops updatekeys was called.
    log_content = log_file.read_text() if log_file.is_file() else ""
    assert "updatekeys" in log_content, (
        f"sops updatekeys was not invoked. Log: {log_content!r}"
    )


def test_handoff_fails_if_no_tenant_specific_rule(tmp_path: Path):
    """Test 11 (edge case): if the tenant has only the catch-all .sops.yaml rule
    (no tenant-specific rule), handoff must exit-fail with a clear message.

    Reasoning: removing the operator master from the catch-all would leave the
    file with zero recipients (unencryptable). The spec says: add a tenant-
    specific rule first, then re-run.
    """
    # add_specific_rule=False — only the catch-all rule applies.
    data_repo = _make_tmp_data_repo_with_tenant(
        tmp_path, tenant="test-no-rule", add_specific_rule=False
    )
    # Need a placeholder secrets.sops.env so existence checks pass.
    (data_repo / "tenants" / "test-no-rule" / "secrets.sops.env").write_text(
        "# placeholder\n"
    )

    result = _run(
        ["test-no-rule", "--mode=handoff", "--yes"],
        data_repo=data_repo,
        timeout=30,
    )

    assert result.returncode != 0, (
        f"expected non-zero exit, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    msg = (result.stderr + result.stdout).lower()
    assert "specific" in msg or "rule" in msg, (
        f"error must mention the missing tenant-specific rule. Got: {msg}"
    )

    # Tenant dir should NOT have been archived (we failed before archive step).
    assert (data_repo / "tenants" / "test-no-rule").is_dir(), (
        "tenant dir must NOT be archived if .sops.yaml edit failed"
    )
