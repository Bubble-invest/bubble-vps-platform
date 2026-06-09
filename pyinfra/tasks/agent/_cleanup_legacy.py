"""Wipe the 8 known plaintext-leak files (SPEC-007 §"Migration path", Step 1
audit). Runs LAST in the agent layer chain — only after _verify.apply()
passes the systemd-driven path is healthy. If verify fails, this never
runs and the legacy plaintext fallback stays intact for rollback.

The 8 files (per the parent agent's task brief):
    1. /home/claude/.secrets                          OPENROUTER_API_KEY
    2. /home/claude/start-claude-agent.sh             OR key inline
    3. /home/claude/.claude/settings.json             ← REWRITTEN by _settings,
                                                        not deleted (the file
                                                        must continue to exist;
                                                        what was IN it leaked).
    4. /home/claude/telegram-mcp-wrapper.sh           bot token
    5. /home/claude/.claude/channels/telegram/.env    plaintext bot token
    6. .../telegram/0.0.6/start-telegram.sh           plugin's startup with token
    7. /home/claude/.claude/projects/.../.jsonl       session transcript w/ token
    8. /home/claude/.claude/history.jsonl             shell-cmd history w/ token

Why this is safe:
    - settings.json (#3) is REWRITTEN by _settings.py, not deleted. Already
      done by the time we reach this module.
    - All others are pure plaintext leak files with no operational role.
    - The session transcript and history.jsonl are records of Claude's
      previous activity on this box — losing them is fine; they'll be
      regenerated as the service runs and operators ask things.

Idempotency:
    files.file(present=False) is the pyinfra primitive for "ensure absent".
    On re-runs the file is already gone → No change. On the corruption case
    (someone manually re-creates the file) → file removed again.

Tmux session kill:
    The legacy `claude-agent` tmux session is also explicitly killed. systemd
    is now the supervisor; the tmux process is orphaned plaintext.
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.facts.server import Command
from pyinfra.operations import files, server

from lib.host_helpers import as_claude


# The 8 plaintext leak files plus the wildcards we can resolve safely.
# Files #1, #2, #4, #5, #6, #8 are exact paths.
# File #7 is a UUID-named .jsonl; we resolve the directory glob at deploy time.
_LEAK_FILES_EXACT = (
    "/home/claude/.secrets",
    "/home/claude/start-claude-agent.sh",
    "/home/claude/telegram-mcp-wrapper.sh",
    "/home/claude/.claude/channels/telegram/.env",
    (
        "/home/claude/.claude/plugins/cache/claude-plugins-official/"
        "telegram/0.0.6/start-telegram.sh"
    ),
    "/home/claude/.claude/history.jsonl",
)

# File #7 — the session transcript dir. We delete every .jsonl under
# /home/claude/.claude/projects/-home-claude-agents-agent-01/ as a glob.
# These are old session logs from the pre-Step-4 setup that may contain
# bot token strings.
_LEGACY_PROJECT_TRANSCRIPT_DIR = (
    "/home/claude/.claude/projects/-home-claude-agents-agent-01"
)


def apply() -> None:
    """Remove the legacy plaintext-leak files. Runs ONLY after _verify passes."""
    # 1-6 of the 8 + the directory probe for #7.
    for path in _LEAK_FILES_EXACT:
        # All leak files live under /home/claude (CLAUDE-OWNED). The deploy
        # connects AS claude → escalate to claude so the unlink succeeds
        # regardless of any 0400/odd modes. `_sudo=True, _sudo_user="claude"`.
        files.file(
            name=f"agent/cleanup: remove plaintext leak {path}",
            path=path,
            present=False,
            _sudo=True,
            _sudo_user="claude",
        )

    # File #7 — the session-transcript directory. We can't use
    # files.directory(present=False) reliably because pyinfra would also fail
    # if any sub-dirs we don't know about exist. Instead: rm -rf the entire
    # legacy transcript directory, idempotent via `test -d` guard.
    transcript_present = host.get_fact(
        Command,
        command=f"test -d {_LEGACY_PROJECT_TRANSCRIPT_DIR} && echo yes || echo no",
    )
    if transcript_present and transcript_present.strip() == "yes":
        server.shell(
            name=(
                "agent/cleanup: remove legacy session-transcript dir "
                f"{_LEGACY_PROJECT_TRANSCRIPT_DIR}"
            ),
            commands=[f"rm -rf {_LEGACY_PROJECT_TRANSCRIPT_DIR}"],
        )

    # Tmux session kill — the old `claude-agent` tmux session ran the legacy
    # plaintext setup. systemd is now the supervisor; a tmux session of the
    # same name is now stale and confusing. tmux kill-session is idempotent
    # via the `2>/dev/null || true` swallow — exit 0 whether the session
    # exists or not.
    # Run AS claude WITHOUT a password (pyinfra connects as claude; bare
    # `su - claude` would self-su-prompt for a password and abort the deploy).
    # The pipeline (`&& echo yes || echo no`) must run in the claude shell, so
    # wrap it in `sh -c '...'` before handing to as_claude — preserves the
    # yes/no stdout this fact depends on.
    tmux_present = host.get_fact(
        Command,
        command=as_claude(
            "sh -c 'tmux has-session -t claude-agent 2>/dev/null && "
            "echo yes || echo no'"
        ),
    )
    if tmux_present and tmux_present.strip() == "yes":
        server.shell(
            name="agent/cleanup: kill legacy tmux session 'claude-agent'",
            commands=[
                as_claude(
                    "sh -c 'tmux kill-session -t claude-agent 2>/dev/null "
                    "|| true'"
                ),
            ],
        )
