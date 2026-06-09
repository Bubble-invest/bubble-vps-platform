"""Provision a per-dept OS user + chown its dirs (Phase-0 least-privilege).

This is the OPT-IN half of the agent_os_user migration. When a concierge is
configured to run as its OWN OS user (e.g. `agent-morty`) instead of the shared
legacy `claude` user, this module:

    1. Creates the system user with `server.user`:
         - system=True            (UID in the system range, no aging)
         - shell=/usr/sbin/nologin (it never logs in interactively)
         - no password            (only reachable via sudo -n -u / systemd User=)
         - NOT in the sudo group   (least privilege — it owns only its own files)
    2. chowns the agent's on-box dirs to that user with `files.directory`:
         - the workdir            (/srv/agents/<persona> for a per-user agent)
         - the per-concierge /run env dir
         - the Telegram channel-state dir
         - the home `.claude` skeleton

Everything is gated on `os_user != LEGACY_OS_USER`: for the legacy `claude`
user this module is a NO-OP, so existing single-tenant deploys are unchanged
until a dept explicitly opts in (the flag). All ops are idempotent.

NOTE (Phase-0 scope): nothing in the live deploy chain passes a non-legacy
os_user yet (the tenant schema has no agent_os_user field — that lands with the
opt-in workstream). This module is the ready-to-wire building block + its TDD
contract; `apply_for_user()` is a pure op-emitter so it can be unit-tested with
the recorder pattern without an SSH connection.
"""

from __future__ import annotations

from pyinfra.operations import files, server

from lib.host_helpers import LEGACY_OS_USER


def apply_for_user(
    os_user: str,
    *,
    workdir: str,
    run_env_dir: str,
    channel_dir: str,
    home: str,
) -> None:
    """Emit the user-creation + ownership ops for ONE per-dept agent.

    Args:
        os_user:     the OS user the agent runs as. If == LEGACY_OS_USER
                     ("claude") this is a NO-OP (legacy behavior unchanged).
        workdir:     the agent's on-box working dir (e.g. /srv/agents/morty).
        run_env_dir: the /run dir holding the decrypted env (e.g.
                     /run/claude-agent-morty).
        channel_dir: the Telegram channel-state dir.
        home:        the agent's home dir (e.g. /home/agent-morty).
    """
    if os_user == LEGACY_OS_USER:
        # Legacy shared-user path — the `claude` user already exists and owns
        # everything. Nothing to provision.
        return

    group = os_user  # primary group shares the user's name (server.user default)

    # 1) Create the system user. system=True keeps it out of the human-UID range,
    #    nologin shell + no password means it's only reachable via systemd's
    #    User= and `sudo -n -u`, and we deliberately do NOT add it to the sudo
    #    group (least privilege). server.user is idempotent.
    server.user(
        name=f"agent/os-user: ensure system user {os_user} (nologin, no sudo)",
        user=os_user,
        system=True,
        shell="/usr/sbin/nologin",
        ensure_home=True,
        home=home,
        # No `groups=["sudo"]` — this user must NEVER be in the sudo group.
        _sudo=True,
    )

    # 2) chown the agent's dirs to its own user. Each is idempotent; present=True
    #    creates the dir if missing so a first-time opt-in is self-bootstrapping.
    for label, path, mode in (
        ("workdir", workdir, "0750"),
        ("run-env dir", run_env_dir, "0750"),
        ("channel dir", channel_dir, "0750"),
        ("home .claude skeleton", f"{home}/.claude", "0755"),
    ):
        files.directory(
            name=f"agent/os-user: chown {label} {path} -> {os_user}",
            path=path,
            user=os_user,
            group=group,
            mode=mode,
            present=True,
            _sudo=True,
        )
