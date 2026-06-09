"""Drop the systemd unit + manage its lifecycle (SPEC-007 §"Service unit").

Two state mutations:
    1. files.template at /etc/systemd/system/claude-agent-<persona>.service
    2. systemd.service to enable + start (and reload daemon if unit changed)

Idempotency:
    - files.template is content-aware (hashes both sides).
    - systemd.service is idempotent in pyinfra: it checks `is-enabled` and
      `is-active` before issuing enable/start.
    - daemon-reload runs unconditionally per pyinfra's gating, but it's
      cheap (~50ms) and silent.

Restart-on-config-change:
    When the .service file changes on a re-deploy, pyinfra's `_if=op.did_change`
    pattern (used in hardening for sshd reload) fires a `systemctl restart`.
    We do the same here: rendering a new unit + daemon-reload + restart all
    follow the template change.
"""

from __future__ import annotations

import operator as op_mod  # avoid shadowing pyinfra `op`
from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import (
    agent_service_name,
    get_tenant_config,
    runtime_env_dir,
    runtime_env_file,
    telegram_channel_dir,
)


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "claude-agent.service.j2"
)
_SOPS_BIN = "/usr/local/bin/sops"
_CLAUDE_BIN = "/usr/bin/claude"
_DEFAULT_CHANNELS = "plugin:telegram@claude-plugins-official"


def _unit_path(persona_name: str) -> str:
    return f"/etc/systemd/system/claude-agent-{persona_name}.service"


def _service_name(persona_name: str) -> str:
    return agent_service_name(persona_name)


def apply() -> None:
    """Drop + lifecycle ONE systemd service PER concierge (SPEC-001 v1.2).

    Each concierge gets its own claude-agent-<name>.service. All concierges
    share the SAME encrypted secrets blob (/etc/bubble/secrets.sops.env) and
    age key, but each service decrypts into its OWN per-concierge runtime env
    dir (primary keeps the historical /run/claude-agent so morty's live unit
    does not churn; others use /run/claude-agent-<name>) so two services never
    clobber each other's tmpfs env file.
    """
    cfg = get_tenant_config(host)
    s = cfg.secrets
    if s is None or not s.enabled:
        # Nothing to wire up if the secrets layer is opt-out.
        return

    for i, concierge in enumerate(cfg.agent.concierges):
        _apply_one(cfg, concierge, s, is_primary=(i == 0))


def _apply_one(cfg, concierge, s, *, is_primary: bool) -> None:
    persona_name = concierge.name
    sysd = concierge.systemd

    unit_path = _unit_path(persona_name)
    service_name = _service_name(persona_name)

    # Per-concierge decrypted runtime env path + its parent /run dir. The
    # primary keeps the tenant-level secrets.decrypted_runtime_path verbatim;
    # additional concierges decrypt into /run/claude-agent-<name>/env.
    rt_dir = runtime_env_dir(
        persona_name, is_primary=is_primary, primary_runtime_path=s.decrypted_runtime_path
    )
    rt_file = runtime_env_file(
        persona_name, is_primary=is_primary, primary_runtime_path=s.decrypted_runtime_path
    )

    # Per-concierge Telegram-token remap (SPEC-001 v1.2 multi-concierge,
    # blocker #1). All concierges share ONE encrypted secrets blob, but the
    # Telegram plugin always reads the bot token from the env var named EXACTLY
    # TELEGRAM_BOT_TOKEN. This concierge's token lives under its
    # channels.telegram.bot_token_secret_ref key in the blob (morty →
    # TELEGRAM_BOT_TOKEN; claudette → CLAUDETTE_TELEGRAM_BOT_TOKEN). We pass that
    # ref into the unit template, which emits a grep/sed remap step IFF the ref
    # is something OTHER than TELEGRAM_BOT_TOKEN — so the primary (morty) unit
    # stays byte-identical to today (no churn), and each additional concierge
    # decrypts ITS OWN token onto TELEGRAM_BOT_TOKEN. When telegram is disabled
    # (no channel/ref), the ref is None → no remap (verbatim decrypt).
    tg = concierge.channels.telegram
    bot_token_secret_ref = tg.bot_token_secret_ref if tg is not None else None

    # Per-concierge TELEGRAM_STATE_DIR (SPEC-021 multi-agent finding). The
    # Telegram MCP plugin keeps ALL of its per-agent state (access.json,
    # approved/, AND bot.pid) under the dir named by TELEGRAM_STATE_DIR,
    # defaulting to the bare ~/.claude/channels/telegram when unset. On a box
    # with TWO concierges (morty + claudette) both pollers would otherwise
    # default to that SAME bare dir → bot.pid collision + getUpdates 409. So we
    # export each concierge's OWN dir. We derive it from the SAME
    # host_helpers.telegram_channel_dir single source _telegram_plugin uses to
    # CREATE the dir, so the exported var can never drift from the created dir.
    # morty → bare telegram/ (live state must not move); others → telegram-<name>/.
    telegram_state_dir = telegram_channel_dir(persona_name)

    # 1) Render the unit file. The template embeds per-concierge paths
    #    (workdir, decrypt target dir + file) so multiple concierges on one box
    #    have their own units without path collisions.
    #
    # Permission mode: hardcoded `--dangerously-skip-permissions` in the
    # template. Per Anthropic docs §"Skip all checks", this is the only way
    # to disable prompts on a headless setup — bypassPermissions CANNOT be
    # activated via settings.json alone, must be a CLI flag at launch.
    # auto mode requires interactive TTY-bound opt-in (also won't work
    # headless). joris-cx33 is an isolated single-tenant VM, fits the
    # doc's "isolated environments" criterion. Future per-tenant override
    # would go through a schema field (SPEC-001 v2).
    unit_op = files.template(
        name=f"agent/systemd: drop unit file {unit_path}",
        src=str(_TEMPLATE_PATH),
        dest=unit_path,
        user="root",
        group="root",
        mode="0644",
        # Template variables (jinja2):
        persona_name=persona_name,
        tenant_name=cfg.tenant_name,
        age_key_path=s.age_key_path,
        encrypted_file_path=s.encrypted_file_path,
        decrypted_runtime_path=rt_file,
        runtime_env_dir=rt_dir,
        bot_token_secret_ref=bot_token_secret_ref,
        telegram_state_dir=telegram_state_dir,
        sops_bin=_SOPS_BIN,
        claude_bin=_CLAUDE_BIN,
        channels=_DEFAULT_CHANNELS,
        restart=sysd.restart,
        restart_sec=sysd.restart_sec,
        nofile_limit=sysd.nofile_limit,
        # The deploy connects AS the claude user (tenant ssh_user: claude).
        # /etc/systemd/system/ is root-owned (user="root" target above), so we
        # MUST escalate to root to write it — `_sudo=True` with NO `_sudo_user`
        # (root, not claude). Without this, files.template fails with
        # `[Errno 13] Permission denied` → `No hosts remaining!`. Same
        # convention as _persona/_settings/_sops_deploy.
        _sudo=True,
    )

    # 2) systemctl daemon-reload — only when the unit file changed. Same
    #    `_if=op.did_change` pattern hardening uses for the sshd config drop
    #    + ssh.service reload. systemctl daemon-reload is a root-only operation
    #    on system units → `_sudo=True` (no `_sudo_user`).
    server.shell(
        name=f"agent/systemd: daemon-reload (only if {service_name} unit changed)",
        commands=["systemctl daemon-reload"],
        _if=unit_op.did_change,
        _sudo=True,
    )

    # 3) Enable + start the service. systemd.service idempotently:
    #      - is-enabled → enable if not
    #      - is-active  → start if not
    #    On a unit-content change we ALSO want a restart, gated by the
    #    template's did_change predicate. Restart-after-enable is safe even
    #    if the service hasn't been started yet (systemctl start is implied
    #    by restart for inactive units).
    # systemctl enable/start on a system unit is root-only → `_sudo=True`.
    systemd.service(
        name=f"agent/systemd: enable + start {service_name}",
        service=service_name,
        enabled=True,
        running=True,
        _sudo=True,
    )

    # systemctl restart on a system unit is root-only → `_sudo=True`.
    server.shell(
        name=f"agent/systemd: restart {service_name} (only if unit changed)",
        commands=[f"systemctl restart {service_name}"],
        _if=unit_op.did_change,
        _sudo=True,
    )
