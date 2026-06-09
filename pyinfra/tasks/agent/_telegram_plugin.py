"""Telegram plugin glue (SPEC-007 + SPEC-009 §"Open question").

Plugin source-of-truth lives at
    /home/claude/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6/

The plugin is fetched/installed by claude itself on first run (when the
plugin is `enabledPlugins` in settings.json). pyinfra does NOT manage the
plugin code — that's claude's job. We DO manage the runtime state directory
(approved chat ids, access policy) so the on-box `claude` user has a writable
home for it.

Token-handling decision (the SPEC-009 open question):
    OPTION C wins. The plugin's server.ts at line ~31 reads the .env file
    only to populate process.env entries that are NOT already set
    (`if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2]`).
    Since systemd injects TELEGRAM_BOT_TOKEN via EnvironmentFile, the env var
    is set in the parent claude process. The MCP server is spawned as a
    child of claude (see `.mcp.json` `command: "bun"`) and inherits its
    environment. So:
        - We do NOT write a .env file.
        - We do NOT symlink /run/claude-agent/env to .env.
        - The bot token reaches the plugin solely via systemd → claude →
          MCP child → process.env.TELEGRAM_BOT_TOKEN.

Verification of Option C is performed in _verify.py (the plugin sends/receives
no traffic before that gate; we trust the source-code reading + smoke test).

Idempotency:
    Only the directory creation is a state mutation. State files (access.json,
    approved/) are managed by the plugin itself once it boots — pyinfra
    leaves them alone.
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.operations import files

from lib.host_helpers import get_tenant_config, telegram_channel_dir


_CHANNELS_DIR = "/home/claude/.claude/channels"


def apply() -> None:
    """Ensure the Telegram plugin's runtime state directories exist.

    This is parent-only — claude creates the actual access.json + approved
    files on first run as it processes pairings. We only need the dirs to
    exist with correct ownership so claude doesn't fail on first write.

    The per-agent state dir is PER-PERSONA (SPEC-021 invariant #4): morty uses
    the bare `telegram/`, other departments use `telegram-<persona>/`. We derive
    it from lib.host_helpers.telegram_channel_dir — the SAME single source the
    watchdog uses for bot.pid — so the dir the plugin writes its bot.pid into
    and the dir the watchdog reads it from can never diverge. This mirrors the
    Mac-side `~/.claude/channels/telegram[-<persona>]` convention and prevents
    two agents on one box from colliding on a single getUpdates poll slot.
    """
    cfg = get_tenant_config(host)

    # The channels parent dir is shared (box-level). Create it once.
    # CLAUDE-OWNED target (/home/claude/.claude/...) → `_sudo=True,
    # _sudo_user="claude"` so the dir ends up owned by claude:claude. The deploy
    # connects AS claude; escalating to the same user lets pyinfra enforce
    # ownership/mode reliably. Mirror of _persona.py's claude-owned writes.
    files.directory(
        name="agent/telegram: ensure ~/.claude/channels exists",
        path=_CHANNELS_DIR,
        user="claude",
        group="claude",
        mode="0755",
        present=True,
        _sudo=True,
        _sudo_user="claude",
    )

    # Per concierge: its OWN channel state dir. morty → bare telegram/, others
    # → telegram-<name>/ (SPEC-021 inv#4, derived from the SAME single-source
    # helper the watchdog uses for bot.pid). Two concierges therefore never
    # share a getUpdates poll slot.
    for concierge in cfg.agent.concierges:
        persona_name = concierge.name
        telegram_dir = telegram_channel_dir(persona_name)
        telegram_approved_dir = f"{telegram_dir}/approved"

        # CLAUDE-OWNED target → `_sudo=True, _sudo_user="claude"`.
        files.directory(
            name=f"agent/telegram: ensure {telegram_dir} exists",
            path=telegram_dir,
            user="claude",
            group="claude",
            mode="0755",
            present=True,
            _sudo=True,
            _sudo_user="claude",
        )

        # CLAUDE-OWNED target → `_sudo=True, _sudo_user="claude"`.
        files.directory(
            name=f"agent/telegram: ensure {telegram_approved_dir} exists",
            path=telegram_approved_dir,
            user="claude",
            group="claude",
            mode="0755",
            present=True,
            _sudo=True,
            _sudo_user="claude",
        )
