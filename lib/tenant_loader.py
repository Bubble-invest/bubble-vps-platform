"""Tenant config loader and validator.

Implements SPEC-001 (tenant.yaml schema) validation rules. Stdlib + pyyaml only.

Public surface:
    - TenantConfigError      (subclass of ValueError)
    - TenantConfig (+ nested HostConfig, HardeningConfig, AgentConfig, AccessConfig)
    - load_tenant(name, data_repo) -> TenantConfig
    - load_tenant_from_path(yaml_path, expected_name=None) -> TenantConfig
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ─── Errors ─────────────────────────────────────────────────────────────────

class TenantConfigError(ValueError):
    """Raised when a tenant.yaml fails to load or validate."""


# ─── Constants ──────────────────────────────────────────────────────────────

_SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TENANT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

_VALID_TENANT_TYPES = ("internal", "client")
_VALID_OS_FAMILIES = ("linux", "macos")
_VALID_OS_DISTROS = ("ubuntu", "debian")
_VALID_PROVIDERS = ("hetzner", "aws", "gcp", "byo")
_VALID_PERMIT_ROOT = ("yes", "no", "prohibit-password")
_VALID_LLM_PROVIDERS = ("openrouter", "anthropic")
_VALID_LLM_AUTH_MODES = ("api_key", "claude_code_subscription")
_VALID_ALLOW_SSH_FROM_KEYWORDS = ("any",)  # otherwise must be CIDR list


# ─── Nested dataclasses ─────────────────────────────────────────────────────

@dataclass
class ContactConfig:
    primary_email: Optional[str] = None
    primary_telegram_user_id: Optional[str] = None


@dataclass
class HostConfig:
    ip: str
    hostname: str
    ssh_user: str
    os_family: str
    provider: str
    ssh_port: int = 22
    os_distro: Optional[str] = None
    os_version: Optional[str] = None
    provider_server_id: Optional[str] = None
    region: Optional[str] = None


@dataclass
class UfwConfig:
    enabled: bool
    allow_ssh_from: Any  # "any" or list[str] of CIDRs


@dataclass
class Fail2banBansConfig:
    maxretry: Optional[int] = None
    findtime_minutes: Optional[int] = None
    bantime_hours: Optional[int] = None


@dataclass
class Fail2banConfig:
    enabled: bool
    sshd_jail: Optional[str] = None
    bans: Optional[Fail2banBansConfig] = None


@dataclass
class SshdConfig:
    permit_root_login: str
    password_authentication: str
    max_auth_tries: Optional[int] = None


@dataclass
class UnattendedUpgradesConfig:
    enabled: bool
    auto_reboot_time: Optional[str] = None


@dataclass
class SwapConfig:
    enabled: bool
    size_gb: Optional[int] = None
    swappiness: Optional[int] = None


@dataclass
class HetznerCloudFirewallConfig:
    enabled: bool
    firewall_id: Optional[str] = None


@dataclass
class SandboxConfig:
    """OS-sandbox (Layer B) hardening. All fields optional — when the whole
    `sandbox` block is absent from tenant.yaml, the hardening module applies the
    default fleet posture (enabled, hard-gate, domains observe-mode). Arrays here
    WIDEN the defaults (Claude Code array-merge semantics); booleans override."""
    enabled: Optional[bool] = None
    fail_if_unavailable: Optional[bool] = None
    extra_allowed_domains: Optional[list] = None
    extra_allow_write: Optional[list] = None


@dataclass
class HardeningConfig:
    ufw: UfwConfig
    fail2ban: Fail2banConfig
    sshd: SshdConfig
    unattended_upgrades: UnattendedUpgradesConfig
    swap: SwapConfig
    hetzner_cloud_firewall: Optional[HetznerCloudFirewallConfig] = None
    sandbox: Optional[SandboxConfig] = None


@dataclass
class AgentInstallConfig:
    claude_code: bool = True
    nodejs_version: str = "22"
    bun: bool = True


@dataclass
class TelegramChannelConfig:
    enabled: bool
    bot_token_secret_ref: Optional[str] = None
    allowed_user_ids: list[str] = field(default_factory=list)


@dataclass
class AgentChannelsConfig:
    telegram: Optional[TelegramChannelConfig] = None


@dataclass
class AgentLLMConfig:
    provider: str
    model: str
    auth_mode: str = "api_key"  # api_key | claude_code_subscription
    api_key_secret_ref: Optional[str] = None  # required if auth_mode=api_key
    base_url: Optional[str] = None


@dataclass
class AgentSystemdConfig:
    restart: str = "on-failure"
    restart_sec: int = 10
    nofile_limit: int = 65536


@dataclass
class ConciergeConfig:
    """A single TRUSTED GENERAL-PURPOSE concierge on a tenant box.

    Multi-concierge per tenant (SPEC-001 v1.2, backlog mission 2026-05-31):
    a tenant supports a LIST of concierges (e.g. bubble-internal runs morty +
    claudette on the same box). Each concierge is a FULLY self-contained agent
    spec — its own name (→ service/channel/workdir derivation), persona_dir,
    LLM model, Telegram channel, and optional systemd overrides. There is no
    shared "agent.llm"/"agent.channels" any more: every concierge carries its
    own, so two concierges on one box can differ in model, bot token, and
    allowed users without cross-contamination.

    Naming convention (SPEC-021 invariant #6): concierges are LAYER-1
    operator-managed agents with NO single mandate. Their on-box workdir is the
    UNPREFIXED `/home/claude/agents/<name>` — the `bubble-ops-` prefix is the
    DEPARTMENT marker (Layer 2) and must NEVER be applied to a concierge name.
    The deploy tasks derive every per-concierge artifact (service
    `claude-agent-<name>.service`, channel `telegram[-<name>]`, runtime env
    `/run/claude-agent-<name>/env`, suffixed watchdog units) from `name`.

    Git-backed workspace (SPEC-001 v1.3):
        A concierge's WORKDIR may be sourced one of TWO ways, and the choice is
        per-concierge:
          - SYNCED (default, MORTY): the data repo holds a curated
            `persona/<name>/workspace/` tree, mirrored to
            `/home/claude/agents/<name>/workspace/` with files.sync(delete=True)
            — the data repo is canonical for that workdir.
          - GIT-BACKED (CLAUDETTE): the concierge's workdir IS its own git repo
            (e.g. https://github.com/vdk888/bubble-claudette-workspace.git). Set
            `workspace_repo` (a git URL) + optional `workspace_branch` (default
            "main"). The deploy CLONES that repo into the workdir
            `/home/claude/agents/<name>` directly (files at top level, NOT under
            a workspace/ subdir) and does NOT run any destructive sync near it.
        These are mutually exclusive: a concierge with `workspace_repo` set must
        NOT also ship a data-repo `persona/<name>/workspace/` tree (ambiguity —
        which is canonical? — is rejected at parse time). Identity (CLAUDE.md →
        agents/<name>.md) and agent-memory/ still come from the data-repo
        persona/ for BOTH models.
    """

    name: str
    persona_dir: str
    channels: AgentChannelsConfig
    llm: AgentLLMConfig
    systemd: AgentSystemdConfig = field(default_factory=AgentSystemdConfig)
    # Git-backed workspace (SPEC-001 v1.3). None → SYNCED model (data-repo
    # persona/<name>/workspace/ tree). A git URL → GIT-BACKED model (clone into
    # the workdir; no workspace/ sync). workspace_branch defaults to "main".
    workspace_repo: Optional[str] = None
    workspace_branch: str = "main"


@dataclass
class AgentConfig:
    """Tenant agent config — install settings + a LIST of concierges.

    SPEC-001 v1.2: `agent.concierges` (a non-empty LIST of ConciergeConfig) is
    the canonical form. The legacy single-concierge form (`agent.persona` +
    tenant-level `agent.channels`/`agent.llm`/`agent.systemd`) is still accepted
    by the parser as a BACK-COMPAT SHIM — it is normalized into a one-element
    `concierges` list at parse time (see `_parse_agent`). Existing client
    tenants on the old schema therefore keep working with zero edits; new and
    migrated tenants use the list form.
    """

    concierges: list[ConciergeConfig]
    install: AgentInstallConfig = field(default_factory=AgentInstallConfig)
    plugins: list[str] = field(default_factory=list)

    @property
    def persona(self) -> ConciergeConfig:
        """Back-compat accessor: the FIRST (primary) concierge.

        Several historical call-sites read `cfg.agent.persona.name`. They are
        being migrated to loop over `cfg.agent.concierges`, but this property
        keeps any not-yet-migrated reader pointing at the primary concierge
        (the first in the list — morty for bubble-internal) instead of breaking.
        Prefer iterating `cfg.agent.concierges` in new code.
        """
        return self.concierges[0]

    @property
    def channels(self) -> AgentChannelsConfig:
        """Back-compat accessor: the primary concierge's channels."""
        return self.concierges[0].channels

    @property
    def llm(self) -> AgentLLMConfig:
        """Back-compat accessor: the primary concierge's LLM config."""
        return self.concierges[0].llm

    @property
    def systemd(self) -> AgentSystemdConfig:
        """Back-compat accessor: the primary concierge's systemd config."""
        return self.concierges[0].systemd


@dataclass
class TailscaleConfig:
    enabled: bool
    authkey_secret_ref: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    accept_routes: bool = False
    advertise_routes: list[str] = field(default_factory=list)


@dataclass
class PhoneHomeConfig:
    enabled: bool
    dashboard_url_secret_ref: Optional[str] = None
    interval_minutes: int = 5


@dataclass
class AccessConfig:
    tailscale: TailscaleConfig
    phone_home: PhoneHomeConfig


@dataclass
class SecretsConfig:
    """SPEC-006 secrets layer config.

    `enabled` flips the SOPS+age task module on/off for this tenant. When
    enabled, `required_keys` are validated post-decrypt; the three path fields
    point at the on-box locations of the age key, encrypted file, and the
    tmpfs-backed runtime decrypt target consumed by systemd.
    """

    enabled: bool
    age_key_path: str = "/etc/age/key.txt"
    encrypted_file_path: str = "/etc/bubble/secrets.sops.env"
    decrypted_runtime_path: str = "/run/claude-agent/env"
    required_keys: list[str] = field(default_factory=list)


# ─── Top-level dataclass ────────────────────────────────────────────────────

@dataclass
class TenantConfig:
    tenant_name: str
    tenant_type: str
    display_name: str
    host: HostConfig
    hardening: HardeningConfig
    agent: AgentConfig
    access: AccessConfig
    contact: ContactConfig = field(default_factory=ContactConfig)
    secrets: Optional[SecretsConfig] = None
    schema_version: int = 1
    created_at: Optional[str] = None
    provisioned_by: Optional[str] = None
    notes: Optional[str] = None
    # Where the tenant.yaml was loaded from — useful for resolving persona_dir.
    tenant_dir: Optional[Path] = None
    raw: dict[str, Any] = field(default_factory=dict)


# ─── Validation helpers ─────────────────────────────────────────────────────

def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise TenantConfigError(f"Missing required field: {where}.{key}")
    return d[key]


def _require_nonempty(d: dict, key: str, where: str) -> Any:
    val = _require(d, key, where)
    if val is None or (isinstance(val, str) and val.strip() == ""):
        raise TenantConfigError(f"Required field is empty: {where}.{key}")
    return val


def _check_enum(value: Any, valid: tuple[str, ...], field_name: str) -> str:
    if value not in valid:
        raise TenantConfigError(
            f"Invalid value for {field_name}: {value!r}. "
            f"Valid values: {', '.join(valid)}"
        )
    return value


def _check_secret_ref(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SECRET_REF_RE.match(value):
        raise TenantConfigError(
            f"Invalid secret ref for {field_name}: {value!r}. "
            f"Must be UPPER_SNAKE_CASE matching {_SECRET_REF_RE.pattern}"
        )
    return value


def _check_ipv4(value: Any, field_name: str) -> str:
    try:
        ipaddress.IPv4Address(str(value))
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise TenantConfigError(
            f"Invalid IPv4 for {field_name}: {value!r} ({exc})"
        ) from exc
    return str(value)


def _ensure_dict(value: Any, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise TenantConfigError(
            f"Expected mapping for {field_name}, got {type(value).__name__}"
        )
    return value


# ─── Section parsers ────────────────────────────────────────────────────────

def _parse_contact(d: Optional[dict]) -> ContactConfig:
    if d is None:
        return ContactConfig()
    d = _ensure_dict(d, "contact")
    return ContactConfig(
        primary_email=d.get("primary_email"),
        primary_telegram_user_id=(
            None
            if d.get("primary_telegram_user_id") is None
            else str(d.get("primary_telegram_user_id"))
        ),
    )


def _parse_host(d: dict) -> HostConfig:
    d = _ensure_dict(d, "host")
    ip = _require_nonempty(d, "ip", "host")
    _check_ipv4(ip, "host.ip")
    hostname = _require_nonempty(d, "hostname", "host")
    ssh_user = _require_nonempty(d, "ssh_user", "host")
    os_family = _check_enum(
        _require_nonempty(d, "os_family", "host"),
        _VALID_OS_FAMILIES,
        "host.os_family",
    )
    provider = _check_enum(
        _require_nonempty(d, "provider", "host"),
        _VALID_PROVIDERS,
        "host.provider",
    )
    ssh_port = int(d.get("ssh_port", 22))

    os_distro = d.get("os_distro")
    os_version = d.get("os_version")
    if os_family == "linux":
        if os_distro is None:
            raise TenantConfigError("host.os_distro is required for os_family=linux")
        _check_enum(os_distro, _VALID_OS_DISTROS, "host.os_distro")
        if os_version is None or str(os_version).strip() == "":
            raise TenantConfigError("host.os_version is required for os_family=linux")

    return HostConfig(
        ip=str(ip),
        hostname=str(hostname),
        ssh_user=str(ssh_user),
        os_family=os_family,
        provider=provider,
        ssh_port=ssh_port,
        os_distro=None if os_distro is None else str(os_distro),
        os_version=None if os_version is None else str(os_version),
        provider_server_id=(
            None if d.get("provider_server_id") is None else str(d.get("provider_server_id"))
        ),
        region=None if d.get("region") is None else str(d.get("region")),
    )


def _parse_hardening(d: dict) -> HardeningConfig:
    d = _ensure_dict(d, "hardening")

    ufw_d = _ensure_dict(_require(d, "ufw", "hardening"), "hardening.ufw")
    ufw = UfwConfig(
        enabled=bool(_require(ufw_d, "enabled", "hardening.ufw")),
        allow_ssh_from=_require(ufw_d, "allow_ssh_from", "hardening.ufw"),
    )
    if isinstance(ufw.allow_ssh_from, str):
        if ufw.allow_ssh_from not in _VALID_ALLOW_SSH_FROM_KEYWORDS:
            # Could also be a single CIDR string; keep permissive but type-check.
            pass
    elif isinstance(ufw.allow_ssh_from, list):
        for item in ufw.allow_ssh_from:
            if not isinstance(item, str):
                raise TenantConfigError(
                    "hardening.ufw.allow_ssh_from list entries must be strings"
                )
    else:
        raise TenantConfigError(
            "hardening.ufw.allow_ssh_from must be 'any' or a list of CIDR strings"
        )

    f2b_d = _ensure_dict(_require(d, "fail2ban", "hardening"), "hardening.fail2ban")
    bans_d = f2b_d.get("bans")
    bans = None
    if bans_d is not None:
        bans_d = _ensure_dict(bans_d, "hardening.fail2ban.bans")
        bans = Fail2banBansConfig(
            maxretry=bans_d.get("maxretry"),
            findtime_minutes=bans_d.get("findtime_minutes"),
            bantime_hours=bans_d.get("bantime_hours"),
        )
    fail2ban = Fail2banConfig(
        enabled=bool(_require(f2b_d, "enabled", "hardening.fail2ban")),
        sshd_jail=f2b_d.get("sshd_jail"),
        bans=bans,
    )

    sshd_d = _ensure_dict(_require(d, "sshd", "hardening"), "hardening.sshd")
    sshd = SshdConfig(
        permit_root_login=_check_enum(
            str(_require(sshd_d, "permit_root_login", "hardening.sshd")),
            _VALID_PERMIT_ROOT,
            "hardening.sshd.permit_root_login",
        ),
        password_authentication=str(
            _require(sshd_d, "password_authentication", "hardening.sshd")
        ),
        max_auth_tries=sshd_d.get("max_auth_tries"),
    )

    uu_d = _ensure_dict(
        _require(d, "unattended_upgrades", "hardening"),
        "hardening.unattended_upgrades",
    )
    unattended = UnattendedUpgradesConfig(
        enabled=bool(_require(uu_d, "enabled", "hardening.unattended_upgrades")),
        auto_reboot_time=uu_d.get("auto_reboot_time"),
    )

    swap_d = _ensure_dict(_require(d, "swap", "hardening"), "hardening.swap")
    swap = SwapConfig(
        enabled=bool(_require(swap_d, "enabled", "hardening.swap")),
        size_gb=swap_d.get("size_gb"),
        swappiness=swap_d.get("swappiness"),
    )

    hcf = None
    hcf_d = d.get("hetzner_cloud_firewall")
    if hcf_d is not None:
        hcf_d = _ensure_dict(hcf_d, "hardening.hetzner_cloud_firewall")
        hcf = HetznerCloudFirewallConfig(
            enabled=bool(_require(hcf_d, "enabled", "hardening.hetzner_cloud_firewall")),
            firewall_id=(
                None
                if hcf_d.get("firewall_id") is None
                else str(hcf_d.get("firewall_id"))
            ),
        )

    # Optional OS-sandbox (Layer B) block. Absent → hardening applies the default
    # fleet posture (the module's _SANDBOX_BLOCK). Present → override booleans /
    # widen arrays.
    sandbox = None
    sb_d = d.get("sandbox")
    if sb_d is not None:
        sb_d = _ensure_dict(sb_d, "hardening.sandbox")
        sandbox = SandboxConfig(
            enabled=sb_d.get("enabled"),
            fail_if_unavailable=sb_d.get("fail_if_unavailable"),
            extra_allowed_domains=sb_d.get("extra_allowed_domains"),
            extra_allow_write=sb_d.get("extra_allow_write"),
        )

    return HardeningConfig(
        ufw=ufw,
        fail2ban=fail2ban,
        sshd=sshd,
        unattended_upgrades=unattended,
        swap=swap,
        hetzner_cloud_firewall=hcf,
        sandbox=sandbox,
    )


def _parse_channels(channels_d: Optional[Any], where: str) -> AgentChannelsConfig:
    """Parse a `channels:` mapping (currently only `telegram`). `where` is the
    dotted path for error messages (e.g. "agent.concierges[0].channels")."""
    channels_d = _ensure_dict(_require_channels(channels_d, where), where)
    telegram = None
    tg_d = channels_d.get("telegram")
    if tg_d is not None:
        tg_where = f"{where}.telegram"
        tg_d = _ensure_dict(tg_d, tg_where)
        enabled = bool(_require(tg_d, "enabled", tg_where))
        bot_token_ref = tg_d.get("bot_token_secret_ref")
        if enabled:
            if bot_token_ref is None:
                raise TenantConfigError(
                    f"{tg_where}.bot_token_secret_ref is required when enabled=true"
                )
            _check_secret_ref(bot_token_ref, f"{tg_where}.bot_token_secret_ref")
        elif bot_token_ref is not None:
            _check_secret_ref(bot_token_ref, f"{tg_where}.bot_token_secret_ref")

        allowed_ids_raw = tg_d.get("allowed_user_ids", [])
        if not isinstance(allowed_ids_raw, list):
            raise TenantConfigError(f"{tg_where}.allowed_user_ids must be a list")
        if enabled and len(allowed_ids_raw) == 0:
            raise TenantConfigError(
                f"{tg_where}.allowed_user_ids must be non-empty when enabled=true"
            )
        allowed_ids = [str(x) for x in allowed_ids_raw]

        telegram = TelegramChannelConfig(
            enabled=enabled,
            bot_token_secret_ref=(None if bot_token_ref is None else str(bot_token_ref)),
            allowed_user_ids=allowed_ids,
        )
    return AgentChannelsConfig(telegram=telegram)


def _require_channels(channels_d: Optional[Any], where: str) -> Any:
    """A channels block is required (even if it only declares disabled telegram).
    Centralized so both the list and back-compat code paths report identically."""
    if channels_d is None:
        raise TenantConfigError(f"Missing required field: {where}")
    return channels_d


def _parse_llm(llm_d: Optional[Any], where: str) -> AgentLLMConfig:
    """Parse an `llm:` mapping. `where` is the dotted path for error messages."""
    if llm_d is None:
        raise TenantConfigError(f"Missing required field: {where}")
    llm_d = _ensure_dict(llm_d, where)
    auth_mode = _check_enum(
        str(llm_d.get("auth_mode", "api_key")),
        _VALID_LLM_AUTH_MODES,
        f"{where}.auth_mode",
    )
    # api_key_secret_ref is required ONLY when auth_mode=api_key. With
    # auth_mode=claude_code_subscription, the agent uses the existing Claude
    # Code login (interactive on the box once) — no API key shipped via SOPS.
    if auth_mode == "api_key":
        api_key_secret_ref = _check_secret_ref(
            _require_nonempty(llm_d, "api_key_secret_ref", where),
            f"{where}.api_key_secret_ref",
        )
    else:
        api_key_secret_ref = None  # explicit None for claude_code_subscription
        if "api_key_secret_ref" in llm_d:
            # Tenant set both — that's a config error. Fail loud.
            raise TenantConfigError(
                f"{where}.api_key_secret_ref must NOT be set when "
                f"{where}.auth_mode=claude_code_subscription"
            )

    return AgentLLMConfig(
        provider=_check_enum(
            str(_require_nonempty(llm_d, "provider", where)),
            _VALID_LLM_PROVIDERS,
            f"{where}.provider",
        ),
        auth_mode=auth_mode,
        api_key_secret_ref=api_key_secret_ref,
        model=str(_require_nonempty(llm_d, "model", where)),
        base_url=(None if llm_d.get("base_url") is None else str(llm_d.get("base_url"))),
    )


def _parse_systemd(systemd_d: Optional[Any], where: str) -> AgentSystemdConfig:
    """Parse a `systemd:` mapping (all fields optional with defaults)."""
    systemd_d = systemd_d or {}
    systemd_d = _ensure_dict(systemd_d, where)
    return AgentSystemdConfig(
        restart=str(systemd_d.get("restart", "on-failure")),
        restart_sec=int(systemd_d.get("restart_sec", 10)),
        nofile_limit=int(systemd_d.get("nofile_limit", 65536)),
    )


def _parse_concierge(d: dict, where: str) -> ConciergeConfig:
    """Parse one concierge entry (name + persona_dir + channels + llm + systemd
    + optional git-backed workspace fields)."""
    d = _ensure_dict(d, where)

    # Optional git-backed workspace (SPEC-001 v1.3). When present, the concierge
    # is deployed by cloning workspace_repo into its workdir; otherwise the
    # data-repo persona/<name>/workspace/ tree is synced. Mutual exclusion with
    # an on-disk workspace/ tree is enforced later in _parse_tenant (where
    # tenant_dir is known to check for the workspace/ subdir).
    workspace_repo = d.get("workspace_repo")
    if workspace_repo is not None:
        if not isinstance(workspace_repo, str) or workspace_repo.strip() == "":
            raise TenantConfigError(
                f"{where}.workspace_repo must be a non-empty git URL string, "
                f"got {workspace_repo!r}"
            )
    workspace_branch = d.get("workspace_branch", "main")
    if not isinstance(workspace_branch, str) or workspace_branch.strip() == "":
        raise TenantConfigError(
            f"{where}.workspace_branch must be a non-empty string, "
            f"got {workspace_branch!r}"
        )

    return ConciergeConfig(
        name=str(_require_nonempty(d, "name", where)),
        persona_dir=str(_require_nonempty(d, "persona_dir", where)),
        channels=_parse_channels(d.get("channels"), f"{where}.channels"),
        llm=_parse_llm(d.get("llm"), f"{where}.llm"),
        systemd=_parse_systemd(d.get("systemd"), f"{where}.systemd"),
        workspace_repo=(None if workspace_repo is None else str(workspace_repo)),
        workspace_branch=str(workspace_branch),
    )


def _parse_agent(d: dict) -> AgentConfig:
    """Parse the `agent:` block.

    SPEC-001 v1.2 multi-concierge: the canonical form is `agent.concierges`
    (a non-empty LIST). For BACK-COMPAT we also accept the legacy single-
    concierge form — `agent.persona` (name + persona_dir) alongside tenant-level
    `agent.channels` / `agent.llm` / `agent.systemd` — and normalize it into a
    one-element list. Exactly ONE of {`concierges`, `persona`} must be present;
    setting both is a config error (ambiguous which wins).

    Validation on the list: ≥1 concierge, and concierge names must be UNIQUE
    (they drive systemd unit / channel / runtime-env / watchdog names — a
    collision would make two concierges fight over the same units). Per-concierge
    persona_dir existence is checked later in `_parse_tenant` where tenant_dir is
    known.
    """
    d = _ensure_dict(d, "agent")

    install_d = d.get("install") or {}
    install_d = _ensure_dict(install_d, "agent.install")
    install = AgentInstallConfig(
        claude_code=bool(install_d.get("claude_code", True)),
        nodejs_version=str(install_d.get("nodejs_version", "22")),
        bun=bool(install_d.get("bun", True)),
    )

    plugins_raw = d.get("plugins", [])
    if not isinstance(plugins_raw, list):
        raise TenantConfigError("agent.plugins must be a list")
    plugins = [str(x) for x in plugins_raw]

    has_concierges = "concierges" in d and d.get("concierges") is not None
    has_persona = "persona" in d and d.get("persona") is not None
    if has_concierges and has_persona:
        raise TenantConfigError(
            "agent: set EITHER agent.concierges (canonical list form) OR the "
            "legacy agent.persona (single form), not both"
        )
    if not has_concierges and not has_persona:
        raise TenantConfigError(
            "agent: must define agent.concierges (a non-empty list) — or the "
            "legacy agent.persona for the single-concierge back-compat form"
        )

    concierges: list[ConciergeConfig] = []
    if has_concierges:
        raw_list = d.get("concierges")
        if not isinstance(raw_list, list) or len(raw_list) == 0:
            raise TenantConfigError(
                "agent.concierges must be a non-empty list of concierge configs"
            )
        for i, entry in enumerate(raw_list):
            concierges.append(_parse_concierge(entry, f"agent.concierges[{i}]"))
    else:
        # BACK-COMPAT SHIM: legacy single-concierge form. `persona` carries
        # name + persona_dir; channels/llm/systemd live at agent level. Wrap
        # the whole thing as a one-element concierges list so all downstream
        # code can iterate uniformly.
        persona_d = _ensure_dict(d.get("persona"), "agent.persona")
        concierges.append(
            ConciergeConfig(
                name=str(_require_nonempty(persona_d, "name", "agent.persona")),
                persona_dir=str(
                    _require_nonempty(persona_d, "persona_dir", "agent.persona")
                ),
                channels=_parse_channels(d.get("channels"), "agent.channels"),
                llm=_parse_llm(d.get("llm"), "agent.llm"),
                systemd=_parse_systemd(d.get("systemd"), "agent.systemd"),
            )
        )

    # Unique-name invariant — concierge names drive every per-concierge on-box
    # artifact (service, channel dir, runtime env, watchdog units). Two
    # concierges with the same name would collide on all of them.
    seen: set[str] = set()
    for c in concierges:
        if c.name in seen:
            raise TenantConfigError(
                f"agent.concierges: duplicate concierge name {c.name!r}; names "
                f"must be unique (they drive systemd unit + channel + runtime "
                f"env + watchdog unit names)"
            )
        seen.add(c.name)

    return AgentConfig(
        install=install,
        concierges=concierges,
        plugins=plugins,
    )


def _parse_secrets(d: Optional[dict]) -> Optional[SecretsConfig]:
    """Parse the optional `secrets:` block (SPEC-006).

    Returns None if the block is absent — the secrets layer is opt-in. When
    present, `enabled` must be a bool, `required_keys` (if set) must be a list
    of UPPER_SNAKE_CASE strings (matches `_SECRET_REF_RE` — same constraint as
    other secret refs in the schema), and the three path fields must be
    absolute paths.
    """
    if d is None:
        return None
    d = _ensure_dict(d, "secrets")

    enabled_raw = _require(d, "enabled", "secrets")
    if not isinstance(enabled_raw, bool):
        raise TenantConfigError(
            f"secrets.enabled must be a bool, got {type(enabled_raw).__name__}"
        )

    age_key_path = str(d.get("age_key_path", "/etc/age/key.txt"))
    encrypted_file_path = str(
        d.get("encrypted_file_path", "/etc/bubble/secrets.sops.env")
    )
    decrypted_runtime_path = str(
        d.get("decrypted_runtime_path", "/run/claude-agent/env")
    )
    for label, val in (
        ("secrets.age_key_path", age_key_path),
        ("secrets.encrypted_file_path", encrypted_file_path),
        ("secrets.decrypted_runtime_path", decrypted_runtime_path),
    ):
        if not val.startswith("/"):
            raise TenantConfigError(
                f"{label} must be an absolute path, got {val!r}"
            )

    required_keys_raw = d.get("required_keys", []) or []
    if not isinstance(required_keys_raw, list):
        raise TenantConfigError("secrets.required_keys must be a list")
    required_keys: list[str] = []
    for item in required_keys_raw:
        if not isinstance(item, str) or not _SECRET_REF_RE.match(item):
            raise TenantConfigError(
                f"Invalid secrets.required_keys entry: {item!r}. "
                f"Each entry must be UPPER_SNAKE_CASE matching {_SECRET_REF_RE.pattern}"
            )
        required_keys.append(item)

    return SecretsConfig(
        enabled=bool(enabled_raw),
        age_key_path=age_key_path,
        encrypted_file_path=encrypted_file_path,
        decrypted_runtime_path=decrypted_runtime_path,
        required_keys=required_keys,
    )


def _parse_access(d: dict) -> AccessConfig:
    d = _ensure_dict(d, "access")

    ts_d = _ensure_dict(_require(d, "tailscale", "access"), "access.tailscale")
    ts_enabled = bool(_require(ts_d, "enabled", "access.tailscale"))
    ts_authkey = ts_d.get("authkey_secret_ref")
    if ts_enabled:
        if ts_authkey is None:
            raise TenantConfigError(
                "access.tailscale.authkey_secret_ref is required when enabled=true"
            )
        _check_secret_ref(ts_authkey, "access.tailscale.authkey_secret_ref")
    elif ts_authkey is not None:
        _check_secret_ref(ts_authkey, "access.tailscale.authkey_secret_ref")
    tags = ts_d.get("tags", []) or []
    if not isinstance(tags, list):
        raise TenantConfigError("access.tailscale.tags must be a list")
    advertise_routes = ts_d.get("advertise_routes", []) or []
    if not isinstance(advertise_routes, list):
        raise TenantConfigError("access.tailscale.advertise_routes must be a list")
    tailscale = TailscaleConfig(
        enabled=ts_enabled,
        authkey_secret_ref=None if ts_authkey is None else str(ts_authkey),
        tags=[str(x) for x in tags],
        accept_routes=bool(ts_d.get("accept_routes", False)),
        advertise_routes=[str(x) for x in advertise_routes],
    )

    ph_d = _ensure_dict(_require(d, "phone_home", "access"), "access.phone_home")
    ph_dashboard = ph_d.get("dashboard_url_secret_ref")
    if ph_dashboard is not None:
        _check_secret_ref(ph_dashboard, "access.phone_home.dashboard_url_secret_ref")
    phone_home = PhoneHomeConfig(
        enabled=bool(_require(ph_d, "enabled", "access.phone_home")),
        dashboard_url_secret_ref=None if ph_dashboard is None else str(ph_dashboard),
        interval_minutes=int(ph_d.get("interval_minutes", 5)),
    )

    return AccessConfig(tailscale=tailscale, phone_home=phone_home)


# ─── Top-level loaders ──────────────────────────────────────────────────────

def _parse_tenant(
    raw: dict,
    *,
    tenant_dir: Optional[Path],
    expected_name: Optional[str],
    check_persona_dir: bool,
) -> TenantConfig:
    if not isinstance(raw, dict):
        raise TenantConfigError(
            f"tenant.yaml top level must be a mapping, got {type(raw).__name__}"
        )

    tenant_name = str(_require_nonempty(raw, "tenant_name", "(root)"))
    if not _TENANT_NAME_RE.match(tenant_name):
        raise TenantConfigError(
            f"Invalid tenant_name {tenant_name!r}: must be lowercase-kebab "
            f"(matches {_TENANT_NAME_RE.pattern})"
        )
    tenant_type = _check_enum(
        str(_require_nonempty(raw, "tenant_type", "(root)")),
        _VALID_TENANT_TYPES,
        "tenant_type",
    )
    display_name = str(_require_nonempty(raw, "display_name", "(root)"))

    contact = _parse_contact(raw.get("contact"))
    if tenant_type == "client":
        if contact.primary_email is None or str(contact.primary_email).strip() == "":
            raise TenantConfigError(
                "contact.primary_email is required (and non-empty) when tenant_type=client"
            )

    if expected_name is not None and tenant_name != expected_name:
        raise TenantConfigError(
            f"tenant_name {tenant_name!r} does not match expected {expected_name!r} "
            f"(directory name)"
        )

    host = _parse_host(_require(raw, "host", "(root)"))
    hardening = _parse_hardening(_require(raw, "hardening", "(root)"))
    agent = _parse_agent(_require(raw, "agent", "(root)"))
    access = _parse_access(_require(raw, "access", "(root)"))
    secrets = _parse_secrets(raw.get("secrets"))

    # persona_dir existence check — once per concierge (only when we know the
    # tenant_dir). Multi-concierge (SPEC-001 v1.2): every concierge's persona_dir
    # must resolve to a real directory inside the tenant dir.
    if check_persona_dir and tenant_dir is not None:
        for concierge in agent.concierges:
            persona_path = (tenant_dir / concierge.persona_dir).resolve()
            if not persona_path.exists() or not persona_path.is_dir():
                raise TenantConfigError(
                    f"concierge {concierge.name!r}: persona_dir does not exist: "
                    f"{concierge.persona_dir!r} "
                    f"(resolved to {persona_path})"
                )
            # Git-backed/synced mutual exclusion (SPEC-001 v1.3): a concierge
            # with workspace_repo set must NOT also ship a data-repo
            # persona/<name>/workspace/ tree — that's ambiguous (which workdir
            # source is canonical?) and the synced delete=True would clobber the
            # cloned repo. Reject, mirroring the persona/concierges both-set rule.
            if concierge.workspace_repo is not None:
                workspace_tree = persona_path / "workspace"
                if workspace_tree.is_dir():
                    raise TenantConfigError(
                        f"concierge {concierge.name!r}: set EITHER workspace_repo "
                        f"(git-backed workdir) OR a data-repo persona/<name>/"
                        f"workspace/ tree, not both. Found workspace_repo "
                        f"({concierge.workspace_repo!r}) AND an on-disk workspace "
                        f"dir at {workspace_tree}. Remove one (git-backed "
                        f"concierges get their workdir from the clone)."
                    )

    schema_version = int(raw.get("schema_version", 1))

    return TenantConfig(
        tenant_name=tenant_name,
        tenant_type=tenant_type,
        display_name=display_name,
        host=host,
        hardening=hardening,
        agent=agent,
        access=access,
        contact=contact,
        secrets=secrets,
        schema_version=schema_version,
        created_at=(None if raw.get("created_at") is None else str(raw.get("created_at"))),
        provisioned_by=(
            None if raw.get("provisioned_by") is None else str(raw.get("provisioned_by"))
        ),
        notes=None if raw.get("notes") is None else str(raw.get("notes")),
        tenant_dir=tenant_dir,
        raw=raw,
    )


def load_tenant(tenant_name: str, data_repo: Path) -> TenantConfig:
    """Load `<data_repo>/tenants/<tenant_name>/tenant.yaml`, validate, return TenantConfig.

    Enforces all SPEC-001 validation rules including:
      - tenant_name matches directory name
      - persona_dir exists relative to tenant dir
    """
    data_repo = Path(data_repo)
    tenant_dir = data_repo / "tenants" / tenant_name
    yaml_path = tenant_dir / "tenant.yaml"
    if not yaml_path.is_file():
        raise TenantConfigError(
            f"tenant.yaml not found for tenant {tenant_name!r}: {yaml_path}"
        )
    with yaml_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    return _parse_tenant(
        raw,
        tenant_dir=tenant_dir,
        expected_name=tenant_name,
        check_persona_dir=True,
    )


def load_tenant_from_path(
    yaml_path: Path,
    expected_name: Optional[str] = None,
) -> TenantConfig:
    """Load and validate a tenant.yaml at an explicit path. For tests / inventory.

    persona_dir existence is only enforced when the file lives in a real
    `tenants/<name>/tenant.yaml` layout (i.e. parent dir matches expected_name).
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.is_file():
        raise TenantConfigError(f"tenant.yaml not found: {yaml_path}")
    with yaml_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    tenant_dir: Optional[Path] = None
    check_persona_dir = False
    if expected_name is not None:
        # If the file lives under tenants/<expected_name>/tenant.yaml, enforce persona check.
        tenant_dir_candidate = yaml_path.parent
        if tenant_dir_candidate.name == expected_name:
            tenant_dir = tenant_dir_candidate
            check_persona_dir = True

    return _parse_tenant(
        raw,
        tenant_dir=tenant_dir,
        expected_name=expected_name,
        check_persona_dir=check_persona_dir,
    )
