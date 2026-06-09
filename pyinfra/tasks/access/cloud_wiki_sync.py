"""Install the cloud-side wiki sync timer (SPEC-020 — Phase 5b).

What this does, idempotently:
    1. Ensures /home/claude/scripts/ exists (mode 0755 owner claude:claude).
    2. Renders /home/claude/scripts/git-credential-helper.sh from a jinja2
       template (mode 0750 owner claude:claude). Tiny ~10 LOC helper that
       reads GITHUB_TOKEN from the calling process env and emits the
       username+password pair per git's credential-helper protocol — keeps
       the token OUT of git's argv and OUT of .git/config.
    3. Renders /home/claude/scripts/cloud-wiki-sync.sh from a jinja2 template
       (mode 0755 owner claude:claude). The bash script does the periodic
       pull --rebase --autostash + commit + push cycle, with conflict-abort
       + Telegram alert on rebase failure. Mirrors the Mac-side
       ~/.claude/scheduled-tasks/wiki-github-sync/sync.sh.
    4. Initial-clone gate: if /home/claude/.claude/agent-memory/shared-wiki/
       lacks a .git directory, perform `git clone` ONCE — using GIT_ASKPASS
       to keep the token out of the URL. After clone, configure
       credential.helper to point at the helper script so subsequent
       pull/push from cron also avoid the URL-token pattern.
    5. Drops the systemd .timer + .service units at
       /etc/systemd/system/cloud-wiki-sync.{timer,service} (mode 0644 owner
       root:root).
    6. systemctl daemon-reload, gated on either unit actually changing on
       disk (lambda-wrapped did_change check — same gotcha as
       telegram_watchdog.py + phone_home.py).
    7. systemctl enable --now cloud-wiki-sync.timer.

NO sudoers needed: the wiki dir is owned by claude, the script runs as
claude (User=claude in the systemd service), and every git operation it
needs is claude-readable/writable.

SPEC-008 hard rule (no plaintext credential to stdout/stderr):
    The bash script reads GITHUB_TOKEN from {decrypted_runtime_path} into
    a shell variable, exports it for git's credential-helper to pick up at
    request time, then `unset` at every exit path. The token NEVER appears
    in argv (which would show up in `ps auxww`) and NEVER persists to
    .git/config (which would be readable by any future agent process).

SPEC-020 hard rule (no token-in-URL):
    The initial clone uses `GIT_ASKPASS=<helper> git clone https://github.com/...`
    — the URL is plain HTTPS with no auth segment. Git invokes the askpass
    helper which echoes the token via the credential protocol. The token
    never touches argv or the URL; .git/config persists nothing more than
    `credential.helper = /home/claude/scripts/git-credential-helper.sh`.

WIKI_DIR layout mirror:
    Mac side:   ~/.claude/agent-memory/shared-wiki/
    Cloud side: /home/claude/.claude/agent-memory/shared-wiki/
    Same path under each user's $HOME — keeps cross-platform tooling sane.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import get_tenant_config


_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_SYNC_SH_TEMPLATE = _TEMPLATES_DIR / "cloud-wiki-sync.sh.j2"
_HELPER_TEMPLATE = _TEMPLATES_DIR / "git-credential-helper.sh.j2"
_TIMER_TEMPLATE = _TEMPLATES_DIR / "cloud-wiki-sync.timer.j2"
_SERVICE_TEMPLATE = _TEMPLATES_DIR / "cloud-wiki-sync.service.j2"

_SCRIPT_DIR = "/home/claude/scripts"
_SYNC_SCRIPT_PATH = "/home/claude/scripts/cloud-wiki-sync.sh"
_CREDENTIAL_HELPER_PATH = "/home/claude/scripts/git-credential-helper.sh"
_TIMER_PATH = "/etc/systemd/system/cloud-wiki-sync.timer"
_SERVICE_PATH = "/etc/systemd/system/cloud-wiki-sync.service"

_WIKI_DIR = "/home/claude/.claude/agent-memory/shared-wiki"
_WIKI_PARENT_DIR = "/home/claude/.claude/agent-memory"
_WIKI_REMOTE_URL = "https://github.com/vdk888/bubble-shared-wiki"
# Lock lives INSIDE /run/cloud-wiki-sync/ (created by systemd's
# RuntimeDirectory= directive, owned by claude). /run/ itself is root-owned
# so we can't mkdir directly there from the User=claude script.
_LOCK_DIR = "/run/cloud-wiki-sync/lock"


def apply() -> None:
    """Install the cloud-side wiki sync timer + service + scripts."""
    cfg = get_tenant_config(host)

    s = cfg.secrets
    if s is None or not s.enabled:
        # Without the secrets layer there's no decrypted env file to read
        # GITHUB_TOKEN from. Skip cleanly — same opt-out shape as the
        # watchdog, audit, and phone-home.
        return
    decrypted_runtime_path = s.decrypted_runtime_path

    joris_telegram_user_id = cfg.contact.primary_telegram_user_id
    if not joris_telegram_user_id:
        # Per SPEC-020, conflict-abort path posts a Telegram alert. Without
        # contact.primary_telegram_user_id, the alert can't escalate. Bail
        # rather than ship a half-broken sync.
        return

    # ─── 1. Ensure /home/claude/scripts/ exists ────────────────────────────
    # CLAUDE-OWNED target (/home/claude/scripts/) — the deploy connects AS
    # claude but pyinfra still needs explicit escalation to set ownership/mode
    # reliably. `_sudo=True, _sudo_user="claude"` so the dir ends up owned by
    # claude:claude (NOT root). Same convention as telegram_watchdog.py.
    files.directory(
        name="access/cloud_wiki_sync: ensure /home/claude/scripts/ exists",
        path=_SCRIPT_DIR,
        present=True,
        mode="0755",
        user="claude",
        group="claude",
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 2. Render git credential helper ───────────────────────────────────
    # Mode 0750: owner (claude) can read+execute; group (claude) can read+
    # execute (so any future tools running as claude can invoke it); world
    # has no access (the helper itself doesn't contain a token, but tightening
    # the surface costs nothing).
    files.template(
        name=f"access/cloud_wiki_sync: render {_CREDENTIAL_HELPER_PATH}",
        src=str(_HELPER_TEMPLATE),
        dest=_CREDENTIAL_HELPER_PATH,
        mode="0750",
        user="claude",
        group="claude",
        # CLAUDE-OWNED target (/home/claude/scripts/...) → escalate to claude so
        # the helper ends up owned by claude:claude. `_sudo=True, _sudo_user="claude"`.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 3. Render the wiki-sync bash script ───────────────────────────────
    files.template(
        name=f"access/cloud_wiki_sync: render {_SYNC_SCRIPT_PATH}",
        src=str(_SYNC_SH_TEMPLATE),
        dest=_SYNC_SCRIPT_PATH,
        mode="0755",
        user="claude",
        group="claude",
        # Template variables (jinja2):
        wiki_dir=_WIKI_DIR,
        wiki_remote_url=_WIKI_REMOTE_URL,
        lock_dir=_LOCK_DIR,
        credential_helper_path=_CREDENTIAL_HELPER_PATH,
        decrypted_runtime_path=decrypted_runtime_path,
        joris_telegram_user_id=joris_telegram_user_id,
        # CLAUDE-OWNED target (/home/claude/scripts/...) → escalate to claude so
        # the script ends up owned by claude:claude. `_sudo=True, _sudo_user="claude"`.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 4. Initial clone (gated: only if .git missing) ────────────────────
    # CRITICAL: token NEVER goes in the URL. We use GIT_ASKPASS pointing at
    # the credential helper, with GITHUB_TOKEN exported into the shell env
    # the helper inherits. Pattern:
    #   TOKEN=$(awk ... env)
    #   GIT_ASKPASS=<helper> GITHUB_TOKEN=$TOKEN GIT_TERMINAL_PROMPT=0 \
    #       git clone https://github.com/vdk888/bubble-shared-wiki <dir>
    # After clone, configure credential.helper for subsequent pulls/pushes.
    # The `test -d ... || (...)` guard makes this idempotent: re-runs after
    # successful clone are no-ops.
    #
    # _sudo + _sudo_user=claude: pyinfra connects as the tenant ssh_user
    # (claude), but we want explicit "run as claude" semantics so future
    # tenants with a different ssh_user don't accidentally clone as the
    # wrong owner. Each shell command runs in its own subshell — `set -e`
    # at the orchestrator level wouldn't propagate, so we chain with `&&`
    # explicitly inside the subshell.
    server.shell(
        name=(
            "access/cloud_wiki_sync: initial clone if "
            f"{_WIKI_DIR}/.git missing"
        ),
        commands=[
            (
                f"test -d {_WIKI_DIR}/.git || ("
                f"  TOKEN=$(awk -F= '/^GITHUB_TOKEN=/{{print $2; exit}}' {decrypted_runtime_path}) && "
                f'  test -n "$TOKEN" && '
                f"  mkdir -p {_WIKI_PARENT_DIR} && "
                f"  cd {_WIKI_PARENT_DIR} && "
                f"  GIT_ASKPASS={_CREDENTIAL_HELPER_PATH} GITHUB_TOKEN=$TOKEN GIT_TERMINAL_PROMPT=0 "
                f"    git clone {_WIKI_REMOTE_URL} shared-wiki && "
                f"  cd shared-wiki && "
                f"  git config credential.helper '{_CREDENTIAL_HELPER_PATH}' && "
                f"  unset TOKEN"
                f")"
            )
        ],
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 5. Drop systemd timer + service units ─────────────────────────────
    timer_op = files.template(
        name=f"access/cloud_wiki_sync: drop {_TIMER_PATH}",
        src=str(_TIMER_TEMPLATE),
        dest=_TIMER_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT-owned target (/etc/systemd/system/...) → `_sudo=True` ALONE (no
        # _sudo_user; the deploy connects AS claude, which cannot write /etc).
        _sudo=True,
    )
    service_op = files.template(
        name=f"access/cloud_wiki_sync: drop {_SERVICE_PATH}",
        src=str(_SERVICE_TEMPLATE),
        dest=_SERVICE_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT-owned target (/etc/systemd/system/...) → `_sudo=True` ALONE.
        _sudo=True,
    )

    # ─── 6. systemctl daemon-reload (gated) ────────────────────────────────
    # Lambda wrap: bound did_change methods are always truthy outside lambda;
    # pyinfra evaluates the lambda at exec time so we get the actual change
    # state. Same pattern as telegram_watchdog + phone_home.
    server.shell(
        name="access/cloud_wiki_sync: systemctl daemon-reload (only if units changed)",
        commands=["systemctl daemon-reload"],
        _if=lambda: timer_op.did_change() or service_op.did_change(),
        # systemctl daemon-reload on system units is root-only → `_sudo=True`.
        _sudo=True,
    )

    # ─── 7. Enable + start the timer ───────────────────────────────────────
    # systemctl enable/start on a system timer is root-only → `_sudo=True`.
    systemd.service(
        name="access/cloud_wiki_sync: enable + start cloud-wiki-sync.timer",
        service="cloud-wiki-sync.timer",
        enabled=True,
        running=True,
        _sudo=True,
    )
    server.shell(
        name="access/cloud_wiki_sync: restart timer (only if timer unit changed)",
        commands=["systemctl restart cloud-wiki-sync.timer"],
        _if=timer_op.did_change,
        # systemctl restart on a system timer is root-only → `_sudo=True`.
        _sudo=True,
    )
