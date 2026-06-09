"""Tests for lib/tenant_loader.py — covers SPEC-001 validation rules.

Run with: python3.12 -m pytest lib/test_tenant_loader.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lib.tenant_loader import (
    TenantConfig,
    TenantConfigError,
    load_tenant,
    load_tenant_from_path,
)


# ─── Fixtures ───────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_REPO = (REPO_ROOT / ".." / "bubble-vps-data").resolve()


def _minimal_valid_yaml() -> str:
    """A minimal valid tenant.yaml body (internal tenant)."""
    return textwrap.dedent("""\
        tenant_name: testenant
        tenant_type: internal
        display_name: Test Tenant
        host:
          ip: 1.2.3.4
          hostname: test-vps
          ssh_user: claude
          os_family: linux
          os_distro: ubuntu
          os_version: "24.04"
          provider: hetzner
        hardening:
          ufw: {enabled: true, allow_ssh_from: any}
          fail2ban: {enabled: true}
          sshd: {permit_root_login: "no", password_authentication: "no"}
          unattended_upgrades: {enabled: true}
          swap: {enabled: true, size_gb: 2}
        agent:
          install: {claude_code: true, bun: true}
          persona: {name: ricky, persona_dir: persona/ricky}
          channels:
            telegram: {enabled: true, bot_token_secret_ref: TELEGRAM_BOT_TOKEN, allowed_user_ids: ["1"]}
          llm: {provider: openrouter, api_key_secret_ref: OPENROUTER_API_KEY, model: "x/y"}
          plugins: []
        access:
          tailscale: {enabled: true, authkey_secret_ref: TAILSCALE_AUTHKEY}
          phone_home: {enabled: true, interval_minutes: 5}
    """)


def _write_tenant_dir(
    tmp_path: Path,
    name: str,
    yaml_body: str,
    *,
    create_persona: bool = True,
) -> Path:
    """Create tenants/<name>/tenant.yaml under tmp_path; optionally a persona dir."""
    tenant_dir = tmp_path / "tenants" / name
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "tenant.yaml").write_text(yaml_body)
    if create_persona:
        persona = tenant_dir / "persona" / "ricky"
        persona.mkdir(parents=True)
        (persona / "CLAUDE.md").write_text("# placeholder\n")
    return tmp_path


# ─── Tests ──────────────────────────────────────────────────────────────────

def test_load_valid_tenant_internal():
    """The real bubble-internal tenant.yaml should load and parse cleanly."""
    cfg = load_tenant("bubble-internal", DATA_REPO)
    assert isinstance(cfg, TenantConfig)
    assert cfg.tenant_name == "bubble-internal"
    assert cfg.tenant_type == "internal"
    assert cfg.display_name == "Bubble Internal"
    assert cfg.host.ip == "178.105.77.178"
    assert cfg.host.hostname == "{{VPS_HOST}}"
    assert cfg.host.ssh_user == "claude"
    assert cfg.host.ssh_port == 22
    assert cfg.host.os_family == "linux"
    assert cfg.host.os_distro == "ubuntu"
    assert cfg.host.provider == "hetzner"
    assert cfg.hardening.ufw.enabled is True
    assert cfg.hardening.fail2ban.enabled is True
    assert cfg.hardening.sshd.permit_root_login == "no"
    assert cfg.hardening.swap.size_gb == 2
    assert cfg.hardening.hetzner_cloud_firewall is not None
    assert cfg.hardening.hetzner_cloud_firewall.firewall_id == "10938002"
    # Step 5a (SPEC-010): persona renamed ricky → morty.
    assert cfg.agent.persona.name == "morty"
    assert cfg.agent.channels.telegram is not None
    assert cfg.agent.channels.telegram.bot_token_secret_ref == "TELEGRAM_BOT_TOKEN"
    assert cfg.agent.channels.telegram.allowed_user_ids == ["{{OPERATOR_CHAT_ID}}"]
    assert cfg.agent.llm.provider == "anthropic"
    assert cfg.agent.llm.auth_mode == "claude_code_subscription"
    assert cfg.agent.llm.api_key_secret_ref is None
    # SPEC-021 (2026-05-31): canonical auto-upgrading alias `opus[1m]` — the
    # "opus" family alias (resolves to the LATEST Opus, deliberate auto-upgrade)
    # + the `[1m]` 1M-context modifier. We do NOT pin a version. The literal
    # "default" was the broken value (production outage root cause).
    assert cfg.agent.llm.model == "opus[1m]"
    assert cfg.access.tailscale.enabled is True
    assert cfg.access.tailscale.authkey_secret_ref == "TAILSCALE_AUTHKEY"
    assert cfg.access.phone_home.enabled is True
    assert cfg.access.phone_home.interval_minutes == 5
    assert cfg.contact.primary_email == "joris@bubbleinvest.fr"
    assert cfg.contact.primary_telegram_user_id == "{{OPERATOR_CHAT_ID}}"


def test_missing_required_field_raises(tmp_path: Path):
    """Removing tenant_name should raise mentioning the field."""
    body = _minimal_valid_yaml().replace("tenant_name: testenant\n", "")
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    assert "tenant_name" in str(exc.value)


def test_invalid_enum_raises(tmp_path: Path):
    """tenant_type=foobar should raise listing valid values."""
    body = _minimal_valid_yaml().replace(
        "tenant_type: internal", "tenant_type: foobar"
    )
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    msg = str(exc.value)
    assert "tenant_type" in msg
    assert "internal" in msg
    assert "client" in msg


def test_tenant_name_mismatch_raises(tmp_path: Path):
    """tenant_name in yaml must match dir name when called via load_tenant()."""
    # Write tenants/expected-name/tenant.yaml but with tenant_name: testenant
    _write_tenant_dir(tmp_path, "expected-name", _minimal_valid_yaml())
    with pytest.raises(TenantConfigError) as exc:
        load_tenant("expected-name", tmp_path)
    msg = str(exc.value)
    assert "testenant" in msg
    assert "expected-name" in msg


def test_invalid_ipv4_raises(tmp_path: Path):
    """host.ip=999.999.999.999 should raise."""
    body = _minimal_valid_yaml().replace("ip: 1.2.3.4", "ip: 999.999.999.999")
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    assert "host.ip" in str(exc.value)


def test_lowercase_secret_ref_raises(tmp_path: Path):
    """bot_token_secret_ref: token (lowercase) should raise."""
    body = _minimal_valid_yaml().replace(
        "bot_token_secret_ref: TELEGRAM_BOT_TOKEN",
        "bot_token_secret_ref: token",
    )
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    msg = str(exc.value)
    assert "bot_token_secret_ref" in msg
    assert "UPPER_SNAKE_CASE" in msg


def test_client_requires_contact_email(tmp_path: Path):
    """tenant_type=client without contact.primary_email should raise."""
    body = _minimal_valid_yaml().replace(
        "tenant_type: internal", "tenant_type: client"
    )
    # _minimal_valid_yaml() has no contact block, so this is the failure case.
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    assert "primary_email" in str(exc.value)


def test_missing_persona_dir_raises(tmp_path: Path):
    """persona_dir pointing to a nonexistent path should raise (via load_tenant)."""
    _write_tenant_dir(
        tmp_path, "testenant", _minimal_valid_yaml(), create_persona=False
    )
    with pytest.raises(TenantConfigError) as exc:
        load_tenant("testenant", tmp_path)
    assert "persona_dir" in str(exc.value)


def test_client_with_email_succeeds(tmp_path: Path):
    """Sanity: tenant_type=client WITH a contact.primary_email should parse."""
    body = _minimal_valid_yaml().replace(
        "tenant_type: internal",
        "tenant_type: client\ncontact:\n  primary_email: ops@example.com",
    )
    yaml_path = tmp_path / "ok.yaml"
    yaml_path.write_text(body)
    cfg = load_tenant_from_path(yaml_path)
    assert cfg.tenant_type == "client"
    assert cfg.contact.primary_email == "ops@example.com"


def test_load_tenant_from_path_no_persona_check_without_expected_name(tmp_path: Path):
    """load_tenant_from_path without expected_name should NOT enforce persona_dir."""
    yaml_path = tmp_path / "standalone.yaml"
    yaml_path.write_text(_minimal_valid_yaml())
    # No persona dir on disk; should still parse.
    cfg = load_tenant_from_path(yaml_path)
    assert cfg.tenant_name == "testenant"


def test_claude_code_subscription_auth_mode(tmp_path: Path):
    """auth_mode=claude_code_subscription must NOT require api_key_secret_ref."""
    body = _minimal_valid_yaml().replace(
        "llm: {provider: openrouter, api_key_secret_ref: OPENROUTER_API_KEY, model: \"x/y\"}",
        "llm: {provider: anthropic, auth_mode: claude_code_subscription, model: \"claude-sonnet-4-7\"}",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    cfg = load_tenant_from_path(yaml_path)
    assert cfg.agent.llm.auth_mode == "claude_code_subscription"
    assert cfg.agent.llm.api_key_secret_ref is None


def test_claude_code_subscription_with_api_key_ref_raises(tmp_path: Path):
    """auth_mode=claude_code_subscription + api_key_secret_ref set = config error."""
    body = _minimal_valid_yaml().replace(
        "llm: {provider: openrouter, api_key_secret_ref: OPENROUTER_API_KEY, model: \"x/y\"}",
        "llm: {provider: anthropic, auth_mode: claude_code_subscription, "
        "api_key_secret_ref: SOME_KEY, model: \"x/y\"}",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="must NOT be set when"):
        load_tenant_from_path(yaml_path)


def test_invalid_auth_mode_raises(tmp_path: Path):
    """auth_mode=foobar should raise listing valid values."""
    body = _minimal_valid_yaml().replace(
        "llm: {provider: openrouter, api_key_secret_ref: OPENROUTER_API_KEY, model: \"x/y\"}",
        "llm: {provider: anthropic, auth_mode: foobar, model: \"x/y\"}",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="auth_mode"):
        load_tenant_from_path(yaml_path)


# ─── Multi-concierge (SPEC-001 v1.2) ─────────────────────────────────────────


def _concierges_yaml() -> str:
    """A minimal valid tenant.yaml using the NEW `agent.concierges` list form
    with TWO concierges (morty + claudette)."""
    return textwrap.dedent("""\
        tenant_name: testenant
        tenant_type: internal
        display_name: Test Tenant
        host:
          ip: 1.2.3.4
          hostname: test-vps
          ssh_user: claude
          os_family: linux
          os_distro: ubuntu
          os_version: "24.04"
          provider: hetzner
        hardening:
          ufw: {enabled: true, allow_ssh_from: any}
          fail2ban: {enabled: true}
          sshd: {permit_root_login: "no", password_authentication: "no"}
          unattended_upgrades: {enabled: true}
          swap: {enabled: true, size_gb: 2}
        agent:
          install: {claude_code: true, bun: true}
          plugins: []
          concierges:
            - name: morty
              persona_dir: persona/morty
              channels:
                telegram: {enabled: true, bot_token_secret_ref: TELEGRAM_BOT_TOKEN, allowed_user_ids: ["1"]}
              llm: {provider: anthropic, auth_mode: claude_code_subscription, model: "opus[1m]"}
            - name: claudette
              persona_dir: persona/claudette
              channels:
                telegram: {enabled: true, bot_token_secret_ref: CLAUDETTE_TELEGRAM_BOT_TOKEN, allowed_user_ids: ["2"]}
              llm: {provider: anthropic, auth_mode: claude_code_subscription, model: "opus[1m]"}
        access:
          tailscale: {enabled: true, authkey_secret_ref: TAILSCALE_AUTHKEY}
          phone_home: {enabled: true, interval_minutes: 5}
    """)


def test_concierges_list_form_parses(tmp_path: Path):
    """The new agent.concierges list form parses into ConciergeConfig list."""
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(_concierges_yaml())
    cfg = load_tenant_from_path(yaml_path)
    assert len(cfg.agent.concierges) == 2
    morty, claudette = cfg.agent.concierges
    assert morty.name == "morty"
    assert morty.persona_dir == "persona/morty"
    assert morty.llm.model == "opus[1m]"
    assert morty.channels.telegram.bot_token_secret_ref == "TELEGRAM_BOT_TOKEN"
    assert morty.channels.telegram.allowed_user_ids == ["1"]
    assert claudette.name == "claudette"
    assert claudette.channels.telegram.bot_token_secret_ref == "CLAUDETTE_TELEGRAM_BOT_TOKEN"
    assert claudette.channels.telegram.allowed_user_ids == ["2"]


def test_concierges_backcompat_property_points_at_first(tmp_path: Path):
    """cfg.agent.persona / .channels / .llm / .systemd alias the FIRST concierge."""
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(_concierges_yaml())
    cfg = load_tenant_from_path(yaml_path)
    assert cfg.agent.persona.name == "morty"
    assert cfg.agent.channels.telegram.bot_token_secret_ref == "TELEGRAM_BOT_TOKEN"
    assert cfg.agent.llm.model == "opus[1m]"
    assert cfg.agent.systemd.restart == "on-failure"


def test_legacy_persona_form_normalizes_to_one_concierge(tmp_path: Path):
    """The legacy single agent.persona form parses to a one-element concierges
    list (back-compat shim) — existing client tenants keep working."""
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(_minimal_valid_yaml())  # uses agent.persona
    cfg = load_tenant_from_path(yaml_path)
    assert len(cfg.agent.concierges) == 1
    assert cfg.agent.concierges[0].name == "ricky"
    assert cfg.agent.concierges[0].persona_dir == "persona/ricky"
    # The tenant-level channels/llm flowed into the single concierge.
    assert cfg.agent.concierges[0].channels.telegram.bot_token_secret_ref == "TELEGRAM_BOT_TOKEN"
    assert cfg.agent.concierges[0].llm.model == "x/y"


def test_both_concierges_and_persona_raises(tmp_path: Path):
    """Setting BOTH agent.concierges AND agent.persona is ambiguous → error."""
    body = _concierges_yaml().replace(
        "  plugins: []\n",
        "  plugins: []\n  persona: {name: extra, persona_dir: persona/extra}\n",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="EITHER agent.concierges"):
        load_tenant_from_path(yaml_path)


def test_neither_concierges_nor_persona_raises(tmp_path: Path):
    """An agent block with neither concierges nor persona → error."""
    body = _minimal_valid_yaml().replace(
        "  persona: {name: ricky, persona_dir: persona/ricky}\n", ""
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="must define agent.concierges"):
        load_tenant_from_path(yaml_path)


def test_empty_concierges_list_raises(tmp_path: Path):
    """agent.concierges: [] → error (≥1 required)."""
    # Take the minimal single-form yaml and swap agent.persona for an empty
    # concierges list (inline flow style — no multi-line block to fight with).
    body = _minimal_valid_yaml().replace(
        "  persona: {name: ricky, persona_dir: persona/ricky}\n",
        "  concierges: []\n",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="non-empty list"):
        load_tenant_from_path(yaml_path)


def test_duplicate_concierge_name_raises(tmp_path: Path):
    """Two concierges with the same name collide on unit/channel names → error."""
    body = _concierges_yaml().replace("name: claudette", "name: morty")
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="duplicate concierge name"):
        load_tenant_from_path(yaml_path)


def test_concierge_missing_persona_dir_on_disk_raises(tmp_path: Path):
    """Per-concierge persona_dir existence is enforced when tenant_dir is known."""
    tenant_dir = tmp_path / "tenants" / "testenant"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "tenant.yaml").write_text(_concierges_yaml())
    # Create only morty's persona dir, NOT claudette's.
    (tenant_dir / "persona" / "morty").mkdir(parents=True)
    (tenant_dir / "persona" / "morty" / "CLAUDE.md").write_text("# m\n")
    with pytest.raises(TenantConfigError) as exc:
        load_tenant("testenant", tmp_path)
    msg = str(exc.value)
    assert "claudette" in msg
    assert "persona_dir" in msg


def test_concierge_lowercase_secret_ref_raises(tmp_path: Path):
    """A lowercase bot_token_secret_ref inside a concierge entry must raise with
    the per-concierge dotted path."""
    body = _concierges_yaml().replace(
        "bot_token_secret_ref: CLAUDETTE_TELEGRAM_BOT_TOKEN",
        "bot_token_secret_ref: token",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    msg = str(exc.value)
    assert "agent.concierges[1].channels.telegram.bot_token_secret_ref" in msg
    assert "UPPER_SNAKE_CASE" in msg


# ─── Git-backed concierge workspace (SPEC-001 v1.3) ──────────────────────────
# A concierge whose workspace IS its own git repo (claudette → her
# bubble-claudette-workspace repo) is deployed by CLONING that repo into the
# workdir, instead of syncing a data-repo persona/<name>/workspace/ tree.
# `workspace_repo` (a git URL) + optional `workspace_branch` (default "main")
# model this. A concierge has EITHER a workspace/ tree OR a workspace_repo —
# never both (ambiguity rejected, mirroring the persona/concierges both-set rule).


def _git_backed_concierge_yaml() -> str:
    """A tenant.yaml whose single concierge (claudette) is git-backed:
    workspace_repo set, no data-repo workspace/ tree."""
    return textwrap.dedent("""\
        tenant_name: testenant
        tenant_type: internal
        display_name: Test Tenant
        host:
          ip: 1.2.3.4
          hostname: test-vps
          ssh_user: claude
          os_family: linux
          os_distro: ubuntu
          os_version: "24.04"
          provider: hetzner
        hardening:
          ufw: {enabled: true, allow_ssh_from: any}
          fail2ban: {enabled: true}
          sshd: {permit_root_login: "no", password_authentication: "no"}
          unattended_upgrades: {enabled: true}
          swap: {enabled: true, size_gb: 2}
        agent:
          install: {claude_code: true, bun: true}
          plugins: []
          concierges:
            - name: claudette
              persona_dir: persona/claudette
              workspace_repo: https://github.com/vdk888/bubble-claudette-workspace.git
              channels:
                telegram: {enabled: true, bot_token_secret_ref: CLAUDETTE_TELEGRAM_BOT_TOKEN, allowed_user_ids: ["2"]}
              llm: {provider: anthropic, auth_mode: claude_code_subscription, model: "opus[1m]"}
        access:
          tailscale: {enabled: true, authkey_secret_ref: TAILSCALE_AUTHKEY}
          phone_home: {enabled: true, interval_minutes: 5}
    """)


def test_concierge_workspace_repo_parses(tmp_path: Path):
    """A concierge with workspace_repo set parses; workspace_branch defaults to main."""
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(_git_backed_concierge_yaml())
    cfg = load_tenant_from_path(yaml_path)
    claudette = cfg.agent.concierges[0]
    assert claudette.name == "claudette"
    assert (
        claudette.workspace_repo
        == "https://github.com/vdk888/bubble-claudette-workspace.git"
    )
    assert claudette.workspace_branch == "main"


def test_concierge_workspace_branch_override_parses(tmp_path: Path):
    """An explicit workspace_branch overrides the main default."""
    # NOTE: textwrap.dedent strips the common leading whitespace, so concierge
    # fields land at 6-space indent in the final string — match that here.
    body = _git_backed_concierge_yaml().replace(
        "workspace_repo: https://github.com/vdk888/bubble-claudette-workspace.git",
        (
            "workspace_repo: https://github.com/vdk888/bubble-claudette-workspace.git\n"
            "      workspace_branch: develop"
        ),
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    cfg = load_tenant_from_path(yaml_path)
    assert cfg.agent.concierges[0].workspace_branch == "develop"


def test_concierge_without_workspace_repo_defaults_none(tmp_path: Path):
    """A non-git concierge (no workspace_repo) → workspace_repo is None,
    workspace_branch defaults still to main (unused)."""
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(_concierges_yaml())  # morty + claudette, neither git-backed
    cfg = load_tenant_from_path(yaml_path)
    for concierge in cfg.agent.concierges:
        assert concierge.workspace_repo is None


def test_concierge_workspace_repo_non_string_raises(tmp_path: Path):
    """workspace_repo must be a string URL when present."""
    body = _git_backed_concierge_yaml().replace(
        "workspace_repo: https://github.com/vdk888/bubble-claudette-workspace.git",
        "workspace_repo: [1, 2, 3]",
    )
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    assert "workspace_repo" in str(exc.value)


def test_concierge_workspace_repo_and_workspace_tree_both_set_raises(tmp_path: Path):
    """A concierge with BOTH workspace_repo AND a data-repo persona workspace/
    tree on disk is ambiguous → rejected (mirrors persona/concierges both-set)."""
    tenant_dir = tmp_path / "tenants" / "testenant"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "tenant.yaml").write_text(_git_backed_concierge_yaml())
    # claudette's persona dir EXISTS and contains a workspace/ subdir → conflict
    # with workspace_repo.
    persona = tenant_dir / "persona" / "claudette"
    persona.mkdir(parents=True)
    (persona / "CLAUDE.md").write_text("# c\n")
    (persona / "workspace").mkdir()
    (persona / "workspace" / "note.md").write_text("# w\n")
    with pytest.raises(TenantConfigError) as exc:
        load_tenant("testenant", tmp_path)
    msg = str(exc.value)
    assert "claudette" in msg
    assert "workspace_repo" in msg
    assert "workspace" in msg


def test_concierge_workspace_repo_with_no_workspace_tree_ok(tmp_path: Path):
    """A git-backed concierge whose persona dir has CLAUDE.md but NO workspace/
    subdir loads fine (this is claudette's real layout)."""
    tenant_dir = tmp_path / "tenants" / "testenant"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "tenant.yaml").write_text(_git_backed_concierge_yaml())
    persona = tenant_dir / "persona" / "claudette"
    persona.mkdir(parents=True)
    (persona / "CLAUDE.md").write_text("# c\n")
    (persona / "agent-memory").mkdir()
    cfg = load_tenant("testenant", tmp_path)
    assert cfg.agent.concierges[0].workspace_repo is not None


# ─── Live bubble-internal: claudette as git-backed concierge (v1.3) ──────────


def test_bubble_internal_loads_with_claudette_git_backed():
    """The REAL bubble-internal tenant.yaml now enables claudette as a
    git-backed concierge (workspace_repo set, persona_dir exists). It must load
    cleanly — persona_dir existence + git/synced mutual-exclusion checks pass."""
    cfg = load_tenant("bubble-internal", DATA_REPO)
    names = [c.name for c in cfg.agent.concierges]
    assert names == ["morty", "claudette"], (
        f"expected morty + claudette concierges, got {names}"
    )
    morty, claudette = cfg.agent.concierges
    # morty is SYNCED (no workspace_repo).
    assert morty.workspace_repo is None
    # claudette is GIT-BACKED.
    assert (
        claudette.workspace_repo
        == "https://github.com/vdk888/bubble-claudette-workspace.git"
    )
    assert claudette.workspace_branch == "main"
    assert claudette.channels.telegram.bot_token_secret_ref == "CLAUDETTE_TELEGRAM_BOT_TOKEN"
    assert claudette.channels.telegram.allowed_user_ids == ["{{OPERATOR_CHAT_ID}}", "{{OPERATOR_2_CHAT_ID}}"]
    assert claudette.llm.model == "opus[1m]"


# ─── secrets: section (SPEC-006) ────────────────────────────────────────────

def test_secrets_config_parses():
    """The real bubble-internal tenant.yaml has a `secrets` block; parse it."""
    cfg = load_tenant("bubble-internal", DATA_REPO)
    assert cfg.secrets is not None
    assert cfg.secrets.enabled is True
    assert cfg.secrets.age_key_path == "/etc/age/key.txt"
    assert cfg.secrets.encrypted_file_path == "/etc/bubble/secrets.sops.env"
    assert cfg.secrets.decrypted_runtime_path == "/run/claude-agent/env"
    assert cfg.secrets.required_keys == [
        "TELEGRAM_BOT_TOKEN",
        # Added 2026-05-31 for SPEC-001 v1.3 (claudette — git-backed concierge
        # #2). Her distinct bot token; the systemd-unit remap exposes it under
        # TELEGRAM_BOT_TOKEN in her per-concierge runtime env file.
        "CLAUDETTE_TELEGRAM_BOT_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "TAILSCALE_AUTHKEY",
        # Added 2026-05-09 for SPEC-015 (Task C — phone-home daemon).
        # Bearer token for the per-tenant phone-home → central dashboard
        # auth. Generated by operator and pasted via operator-set-secret.sh.
        "PHONEHOME_TOKEN",
        # Added 2026-05-09 for SPEC-020 (Phase 5b — cloud wiki sync).
        # Fine-grained GitHub PAT scoped to Contents R+W on
        # vdk888/bubble-shared-wiki. Consumed by /home/claude/scripts/
        # cloud-wiki-sync.sh via the GIT_ASKPASS credential helper.
        "GITHUB_TOKEN",
    ]


def test_secrets_invalid_required_key_raises(tmp_path: Path):
    """A lower_case entry in secrets.required_keys should raise."""
    body = _minimal_valid_yaml() + textwrap.dedent("""\
        secrets:
          enabled: true
          required_keys:
            - lower_case
    """)
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError) as exc:
        load_tenant_from_path(yaml_path)
    msg = str(exc.value)
    assert "secrets.required_keys" in msg
    assert "UPPER_SNAKE_CASE" in msg


def test_secrets_absent_returns_none(tmp_path: Path):
    """A tenant.yaml without `secrets:` should leave cfg.secrets == None."""
    yaml_path = tmp_path / "tenant.yaml"
    yaml_path.write_text(_minimal_valid_yaml())  # no secrets block
    cfg = load_tenant_from_path(yaml_path)
    assert cfg.secrets is None


def test_secrets_invalid_enabled_type_raises(tmp_path: Path):
    """secrets.enabled must be a bool, not a string."""
    body = _minimal_valid_yaml() + textwrap.dedent("""\
        secrets:
          enabled: "yes"
    """)
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="secrets.enabled must be a bool"):
        load_tenant_from_path(yaml_path)


def test_secrets_relative_path_raises(tmp_path: Path):
    """secrets.age_key_path must be absolute."""
    body = _minimal_valid_yaml() + textwrap.dedent("""\
        secrets:
          enabled: true
          age_key_path: etc/age/key.txt
    """)
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(body)
    with pytest.raises(TenantConfigError, match="must be an absolute path"):
        load_tenant_from_path(yaml_path)
