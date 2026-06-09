"""Helpers for pyinfra task modules.

Per SPEC-003, the inventory exposes a minimal `host.data` dict. Tasks that
need the full tenant config call get_tenant_config() to load it on demand
from the data repo, scoped to the current host.

This keeps PII and per-tenant metadata out of pyinfra's debug output and
shared logs while still giving tasks ergonomic access to whatever they need.
"""

from __future__ import annotations

import os
from pathlib import Path

from lib.tenant_loader import TenantConfig, load_tenant


_DEFAULT_DATA_REPO_RELATIVE = "../bubble-vps-data"


def _resolve_data_repo() -> Path:
    """Same resolution as inventory.py — share the env-var convention."""
    raw = os.environ.get("BUBBLE_DATA_REPO")
    if raw:
        return Path(raw).expanduser().resolve()
    # platform repo root = parent of lib/
    platform_root = Path(__file__).resolve().parent.parent
    return (platform_root / _DEFAULT_DATA_REPO_RELATIVE).resolve()


def get_tenant_config(host) -> TenantConfig:
    """Load the current host's tenant.yaml on demand.

    Args:
        host: pyinfra `host` object (must have `host.data.tenant_name`)

    Returns:
        Fully validated TenantConfig.

    Raises:
        TenantConfigError: if the YAML is invalid (should never happen here
            because inventory.py already validated, but tasks should still
            handle the error gracefully if the data repo changes mid-deploy).

    Loaded fresh each call. No caching across operations — pyinfra runs tasks
    in separate processes per host anyway, so caching wouldn't help.
    """
    tenant_name = host.data.tenant_name
    data_repo = _resolve_data_repo()
    return load_tenant(tenant_name, data_repo)


def get_persona_dir(host) -> Path:
    """Operator-Mac-side absolute path to this host's persona directory."""
    return Path(host.data.persona_dir)


def get_secrets_file(host) -> Path:
    """Operator-Mac-side absolute path to this host's secrets.sops.env.

    File may not exist yet at Step 1. Existence is checked by the secrets
    task (Step 3+).
    """
    return Path(host.data.secrets_file)


# ─── Telegram channel-dir derivation (SPEC-021 invariant #4) ─────────────────
#
# The Telegram MCP plugin (server.ts) stores ALL of its per-agent runtime state
# — access.json, approved/, inbox/, .env AND bot.pid — under a single directory
# it reads from the TELEGRAM_STATE_DIR environment variable, defaulting to
# ~/.claude/channels/telegram when that var is unset (see the plugin's
# `const STATE_DIR = process.env.TELEGRAM_STATE_DIR ?? join(homedir(),
# '.claude','channels','telegram')`). bot.pid lives at
# `join(STATE_DIR, 'bot.pid')`.
#
# On a single-agent box that default (bare `telegram/`) is fine. But Joris's
# fleet runs MULTIPLE agents per box, and each agent MUST have its own channel
# dir + its own bot token — otherwise two pollers fight over the same
# `getUpdates` long-poll slot and Telegram returns 409 Conflict (the exact bug
# we've hit repeatedly on the Mac with orphaned bun processes). The Mac-side
# convention encodes that isolation as `~/.claude/channels/telegram-<persona>`,
# with the historical exception that the ORIGINAL agent ("morty" on the cloud
# box, "main" on the Mac) keeps the bare `telegram/` dir.
#
# This helper is the SINGLE SOURCE OF TRUTH for that derivation. Both the
# plugin-state-dir creation (_telegram_plugin) and the watchdog's bot.pid path
# (telegram_watchdog) MUST go through it so they can never disagree about where
# bot.pid lives — a disagreement is exactly what made the watchdog look for
# morty's bot.pid in every other agent's recovery (the FIX-2 bug).

# ─── agent_os_user concept (Phase-0 least-privilege migration) ────────────────
#
# An agent's OS identity is parametrized by `os_user`: the Unix user it runs as,
# its primary group (same name), and its home dir (/home/<os_user>). The LEGACY
# default is the shared `claude` user — every helper that takes an `os_user`
# argument defaults to it, so existing single-tenant behavior (and every golden
# file) stays byte-identical until a caller explicitly opts in to a per-dept user
# (e.g. `agent-morty`).
#
# KEY DESIGN DECISION (already made): when a NON-legacy user is used, an agent's
# workdir moves OUT of its home to /srv/agents/<persona>. This decouples the
# workdir from the home dir so the session-transcript path (derived from the
# workdir by '/'→'-') has a SINGLE rename point. Legacy stays under the home at
# /home/claude/agents/<persona>.

LEGACY_OS_USER = "claude"


def agent_home(os_user: str = LEGACY_OS_USER) -> str:
    """Absolute home directory for an agent's OS user.

    claude (legacy) → /home/claude
    agent-morty     → /home/agent-morty
    """
    return f"/home/{os_user}"


_CHANNELS_BASE = "/home/claude/.claude/channels"

# The persona whose channel dir is the bare `telegram/` (no `-<persona>`
# suffix). This is the original/default agent on the box — its plugin runs
# with TELEGRAM_STATE_DIR unset (or pointed at the bare dir), matching the
# plugin's built-in default. New departments added later get a suffixed dir.
_DEFAULT_CHANNEL_PERSONA = "morty"


def telegram_channel_dir_name(persona_name: str) -> str:
    """Return the channel DIRECTORY NAME (not full path) for a persona.

    morty            → "telegram"            (the plugin's built-in default)
    <other persona>  → "telegram-<persona>"  (per-agent isolation)

    Mirrors the Mac-side `~/.claude/channels/telegram[-<name>]` convention so
    the cloud box and the operator's Mac name channels identically.
    """
    if persona_name == _DEFAULT_CHANNEL_PERSONA:
        return "telegram"
    return f"telegram-{persona_name}"


def telegram_channel_dir(persona_name: str) -> str:
    """Absolute path to a persona's Telegram channel state dir on the box.

    e.g. morty → /home/claude/.claude/channels/telegram
         maya  → /home/claude/.claude/channels/telegram-maya
    """
    return f"{_CHANNELS_BASE}/{telegram_channel_dir_name(persona_name)}"


def telegram_bot_pid_file(persona_name: str) -> str:
    """Absolute path to a persona's bot.pid (the plugin's liveness marker).

    The watchdog uses this as its PRIMARY health signal. It MUST point at the
    SAME dir the plugin writes to — see telegram_channel_dir's docstring.
    """
    return f"{telegram_channel_dir(persona_name)}/bot.pid"


# ─── Per-concierge on-box artifact derivations (SPEC-001 v1.2 multi-concierge) ─
#
# A tenant box may host MULTIPLE concierges (e.g. bubble-internal = morty +
# claudette). Every per-concierge artifact MUST be derived from the concierge
# NAME so two concierges on one box never collide. These helpers are the SINGLE
# SOURCE OF TRUTH for those derivations — the agent tasks (_persona, _settings,
# _systemd_unit, _telegram_plugin, _verify) and the watchdog all import them, so
# a future packaging change (Tenant-as-a-Service: a client concierge "Sandra")
# only has to touch this one file.
#
# SPEC-021 invariant #6: concierges are LAYER-1 agents with the UNPREFIXED
# workdir /home/claude/agents/<name>. The `bubble-ops-` prefix is the DEPARTMENT
# marker (Layer 2) and is NEVER applied here — these helpers take a concierge
# name and must receive an already-unprefixed name.


def agent_workdir(persona_name: str, os_user: str = LEGACY_OS_USER) -> str:
    """The concierge's on-box working directory — UNPREFIXED (SPEC-021 inv#6).

    LEGACY (os_user=claude):
        morty → /home/claude/agents/morty
        claudette → /home/claude/agents/claudette

    PER-USER (os_user != claude):
        morty → /srv/agents/morty
        maya  → /srv/agents/maya

    This is the systemd unit's WorkingDirectory= and the dir claude derives its
    session-transcript dir from (by replacing every '/' with '-').

    Phase-0 migration decision: for a per-dept user the workdir lives under
    /srv/agents/<persona> (NOT the user's home) so the workdir is decoupled from
    the home dir and the session-transcript path has a single rename point.
    """
    if os_user == LEGACY_OS_USER:
        return f"/home/{LEGACY_OS_USER}/agents/{persona_name}"
    return f"/srv/agents/{persona_name}"


def agent_service_name(persona_name: str) -> str:
    """systemd service unit name for a concierge's agent.

    e.g. morty → claude-agent-morty.service
    """
    return f"claude-agent-{persona_name}.service"


def agent_session_projects_dir(persona_name: str, os_user: str = LEGACY_OS_USER) -> str:
    """claude's per-project session-transcript dir for this concierge.

    claude names it by taking the absolute WorkingDirectory and replacing every
    '/' with '-' (leading slash → leading '-'). The watchdog's 401-probe tails
    the newest jsonl here, so it MUST derive from the SAME workdir as the unit's
    WorkingDirectory= (SPEC-021 invariant #4d).

    LEGACY (os_user=claude):
        morty → /home/claude/.claude/projects/-home-claude-agents-morty
    PER-USER (os_user != claude):
        morty (agent-morty) → /home/agent-morty/.claude/projects/-srv-agents-morty

    Both the .claude base (the user's HOME) and the project-name segment (the
    workdir with '/'→'-') follow os_user automatically.
    """
    return (
        f"{agent_home(os_user)}/.claude/projects/"
        + agent_workdir(persona_name, os_user).replace("/", "-")
    )


def runtime_env_dir(persona_name: str, *, is_primary: bool, primary_runtime_path: str) -> str:
    """The /run directory that holds a concierge's decrypted env file.

    primary  → dirname(primary_runtime_path), e.g. /run/claude-agent
    other    → /run/claude-agent-<name>       (matches claudette hand-deploy)
    """
    if is_primary:
        # Strip the trailing /env (or whatever filename) → the runtime dir.
        return primary_runtime_path.rsplit("/", 1)[0]
    return f"/run/claude-agent-{persona_name}"


def runtime_env_file(persona_name: str, *, is_primary: bool, primary_runtime_path: str) -> str:
    """The decrypted env FILE path for a concierge.

    primary  → primary_runtime_path verbatim (e.g. /run/claude-agent/env) so
               morty's live unit is unchanged.
    other    → /run/claude-agent-<name>/env
    """
    if is_primary:
        return primary_runtime_path
    return f"{runtime_env_dir(persona_name, is_primary=False, primary_runtime_path=primary_runtime_path)}/env"


# ─── Watchdog unit naming (persona-suffixed for multi-concierge boxes) ────────
#
# SPEC-021's "Architecture note" flagged that the watchdog units were
# UN-suffixed (one-agent-per-box assumption). Multi-concierge per box REQUIRES
# suffixing so morty's and claudette's watchdogs don't collide. We suffix EVERY
# concierge UNIFORMLY (migration-b — see the deploy task docstring): morty also
# moves from the old bare `telegram-watchdog.*` to `telegram-watchdog-morty.*`.
# This keeps the naming scheme regular (no "morty is special" branch) at the
# cost of a one-time cleanup of morty's old un-suffixed units on the live box.


def as_user(os_user: str, cmd: str) -> str:
    """Wrap a shell command so it runs AS `os_user` WITHOUT a password.

    Phase-0 least-privilege migration: the parametric form of `as_claude`. The
    deploy historically always connected (and ran commands) as the shared
    `claude` user; this variant lets a per-dept user (`agent-morty`, …) be the
    target instead. `as_claude(cmd)` is now the legacy alias `as_user("claude",
    cmd)` so every existing call site is unchanged. The NOPASSWD reliance is NOT
    removed here (that's a later phase) — this just makes the target parametric.

    Why this exists (the bug it fixes):
        The pyinfra deploy connects to the box AS the `claude` user
        (tenant.yaml → host.ssh_user: claude). Several agent tasks historically
        ran commands via `su - claude -c '...'`. But `su - claude` when you are
        ALREADY claude PROMPTS FOR A PASSWORD (self-su needs auth) and aborts:
        `su: Authentication failure` → `pyinfra error: No hosts remaining!`.
        This blocked EVERY deploy. (Same class of bug fixed in the
        bubble-ops-loop deploy script on 2026-05-31.)

    The fix:
        pyinfra runs as the login user, so when that login user IS the target
        the command runs DIRECTLY (no su). To stay correct if a future deploy
        connects as a different user (e.g. root), we branch on the login user at
        runtime — a guard that needs NO password either way:

            if [ "$(id -un)" = <USER> ]; then <CMD>; else sudo -n -u <USER> <CMD>; fi

        - When already <USER>: runs <CMD> directly (no privilege change, no auth).
        - Otherwise: `sudo -n -u <USER>` drops to <USER> NON-interactively
          (-n = never prompt; NOPASSWD-safe, verified on the box).
        - NEVER bare `su - <USER>` (that's the bug).

    Quoting note:
        `cmd` is interpolated verbatim into BOTH branches. Callers that need a
        pipeline (e.g. `curl ... | bash`, or `echo <b64> | base64 -d | python3 -`)
        must pass it pre-wrapped in `sh -c '...'` so the WHOLE pipeline runs in
        the target shell — otherwise `sudo -n -u <USER>` would only run the first
        pipe segment as <USER>. The single-arg `sh -c '...'` form keeps the pipe
        intact through the sudo boundary and survives this wrapper's quoting
        (the wrapper adds no extra quotes around `cmd`).
    """
    return (
        f'if [ "$(id -un)" = {os_user} ]; then {cmd}; '
        f"else sudo -n -u {os_user} {cmd}; fi"
    )


def as_claude(cmd: str) -> str:
    """Legacy alias for `as_user("claude", cmd)` — see as_user's docstring.

    Kept so every existing call site (_install, _settings, _cleanup_legacy) and
    the as_claude golden-behavior tests stay unchanged. New code targeting a
    per-dept user should call `as_user(os_user, cmd)` directly.
    """
    return as_user(LEGACY_OS_USER, cmd)


def watchdog_unit_basename(persona_name: str) -> str:
    """Base name for a concierge's watchdog units/script/sudoers/runtime-dir.

    Always persona-suffixed → telegram-watchdog-<name>. Used to derive:
      - /etc/systemd/system/telegram-watchdog-<name>.{timer,service}
      - /home/claude/scripts/telegram-watchdog-<name>.sh
      - /etc/sudoers.d/claude-telegram-watchdog-<name>
      - RuntimeDirectory=telegram-watchdog-<name> → /run/telegram-watchdog-<name>/

    Suffixed for ALL concierges (including morty) so no two concierges on one
    box can collide on unit names (SPEC-021 multi-agent follow-up, migration-b).
    """
    return f"telegram-watchdog-{persona_name}"
