"""Phase-0 Workstream-A: user-parametric deploy (least-privilege migration).

These tests pin the `agent_os_user` concept: the OS user/group/home an agent
runs as. The DEFAULT is the LEGACY shared `claude` user (so every existing
test + golden file stays byte-identical — current behavior is unchanged until a
caller explicitly opts in to a per-dept user).

When a non-legacy user (e.g. `agent-morty`) IS passed, the helpers and the
rendered systemd unit switch to the per-user paths:

    - home base               → /home/<os_user>
    - workdir                 → /srv/agents/<persona>   (decoupled from home so
                                the session-transcript path has ONE rename point)
    - session-transcript dir  → /srv/agents/<persona> with '/' → '-'
    - systemd User=/Group=     → <os_user>
    - WorkingDirectory=        → /srv/agents/<persona>

Legacy (`claude`) keeps everything where it is:

    - workdir                 → /home/claude/agents/<persona>
    - session-transcript dir  → /home/claude/.claude/projects/-home-claude-agents-<persona>

Run with: .venv/bin/python -m pytest lib/test_agent_os_user.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyinfra" / "templates"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ─── The flag defaults to legacy ─────────────────────────────────────────────


def test_legacy_os_user_constant_is_claude():
    """The single source of truth for the legacy default is the shared `claude`
    user. Every os_user-aware helper falls back to it when unset."""
    from lib.host_helpers import LEGACY_OS_USER

    assert LEGACY_OS_USER == "claude"


def test_agent_home_defaults_to_claude():
    from lib.host_helpers import agent_home

    assert agent_home() == "/home/claude"
    assert agent_home("claude") == "/home/claude"


def test_agent_home_parametric():
    from lib.host_helpers import agent_home

    assert agent_home("agent-morty") == "/home/agent-morty"
    assert agent_home("agent-maya") == "/home/agent-maya"


# ─── agent_workdir: legacy under home, per-user under /srv/agents ─────────────


def test_agent_workdir_legacy_unchanged():
    """No os_user (or os_user=claude) → the historical /home/claude/agents/<name>
    path. This is the existing single-arg contract (test_agent_layer pins it too)."""
    from lib.host_helpers import agent_workdir

    assert agent_workdir("morty") == "/home/claude/agents/morty"
    assert agent_workdir("claudette") == "/home/claude/agents/claudette"
    assert agent_workdir("morty", "claude") == "/home/claude/agents/morty"


def test_agent_workdir_per_user_moves_to_srv():
    """A non-legacy os_user moves the workdir to /srv/agents/<persona> — decoupled
    from the home dir so the session-transcript path has a single rename point."""
    from lib.host_helpers import agent_workdir

    assert agent_workdir("morty", "agent-morty") == "/srv/agents/morty"
    assert agent_workdir("maya", "agent-maya") == "/srv/agents/maya"
    # The persona name (not the os_user) names the workdir leaf.
    assert agent_workdir("claudette", "agent-claudette") == "/srv/agents/claudette"


# ─── session-transcript dir follows the workdir + the home base ──────────────


def test_session_projects_dir_legacy_unchanged():
    from lib.host_helpers import agent_session_projects_dir

    assert agent_session_projects_dir("morty") == (
        "/home/claude/.claude/projects/-home-claude-agents-morty"
    )
    assert agent_session_projects_dir("claudette") == (
        "/home/claude/.claude/projects/-home-claude-agents-claudette"
    )


def test_session_projects_dir_per_user_derives_from_srv_workdir():
    """For a per-user agent the projects dir lives under that user's HOME .claude,
    and the project-name segment is the /srv workdir with '/' → '-'."""
    from lib.host_helpers import agent_session_projects_dir

    assert agent_session_projects_dir("morty", "agent-morty") == (
        "/home/agent-morty/.claude/projects/-srv-agents-morty"
    )
    assert agent_session_projects_dir("maya", "agent-maya") == (
        "/home/agent-maya/.claude/projects/-srv-agents-maya"
    )


# ─── as_user: parametric run-as guard; as_claude is the legacy alias ─────────


def test_as_user_branches_on_login_user():
    from lib.host_helpers import as_user

    out = as_user("agent-morty", "echo hi")
    assert 'if [ "$(id -un)" = agent-morty ]; then echo hi;' in out
    assert "else sudo -n -u agent-morty echo hi; fi" in out


def test_as_user_claude_matches_as_claude_legacy_alias():
    """as_claude(cmd) must be exactly as_user('claude', cmd) — the legacy alias
    so all existing as_claude call sites are unchanged."""
    from lib.host_helpers import as_claude, as_user

    for cmd in ("echo hi", "sh -c 'a | b | c'", "systemctl status x"):
        assert as_claude(cmd) == as_user("claude", cmd)


def test_as_user_never_emits_bare_su():
    from lib.host_helpers import as_user

    assert "su - " not in as_user("agent-morty", "echo hi")


def test_as_user_interpolates_command_verbatim_both_branches():
    from lib.host_helpers import as_user

    out = as_user("agent-maya", "sh -c 'do | a | pipe'")
    assert out.count("sh -c 'do | a | pipe'") == 2


# ─── systemd unit template: User/Group/WorkingDirectory are parametric ───────


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


_BASE_RENDER_KWARGS = dict(
    persona_name="morty",
    tenant_name="bubble-internal",
    age_key_path="/etc/age/key.txt",
    encrypted_file_path="/etc/bubble/secrets.sops.env",
    decrypted_runtime_path="/run/claude-agent/env",
    runtime_env_dir="/run/claude-agent",
    bot_token_secret_ref="TELEGRAM_BOT_TOKEN",
    telegram_state_dir="/home/claude/.claude/channels/telegram",
    sops_bin="/usr/local/bin/sops",
    claude_bin="/usr/bin/claude",
    channels="plugin:telegram@claude-plugins-official",
    restart="on-failure",
    restart_sec=10,
    nofile_limit=65536,
)


def _render(**overrides) -> str:
    kwargs = dict(_BASE_RENDER_KWARGS)
    kwargs.update(overrides)
    return _jinja_env().get_template("claude-agent.service.j2").render(**kwargs)


def test_unit_default_render_is_legacy_claude():
    """Rendering WITHOUT the new os_user/group/workdir vars yields the legacy
    User=claude / Group=claude / WorkingDirectory=/home/claude/agents/<name>.
    This is what keeps the golden byte-identical."""
    rendered = _render()
    assert "User=claude\n" in rendered
    assert "Group=claude\n" in rendered
    assert "WorkingDirectory=/home/claude/agents/morty\n" in rendered


def test_unit_per_user_render_overrides_user_group_workdir():
    """When os_user/os_group/agent_workdir vars ARE passed, the rendered unit
    uses them. This is the opt-in per-dept-user form."""
    rendered = _render(
        os_user="agent-morty",
        os_group="agent-morty",
        agent_workdir="/srv/agents/morty",
    )
    assert "User=agent-morty\n" in rendered
    assert "Group=agent-morty\n" in rendered
    assert "WorkingDirectory=/srv/agents/morty\n" in rendered
    # The legacy literals must be gone in the per-user render.
    assert "User=claude\n" not in rendered
    assert "WorkingDirectory=/home/claude/agents/morty\n" not in rendered


def test_unit_per_user_render_chowns_decrypted_env_to_os_user():
    """The ExecStartPre chown of the decrypted runtime env must target the
    agent's OWN user when opted in (so a per-dept user can read its env file)."""
    rendered = _render(
        os_user="agent-morty",
        os_group="agent-morty",
        agent_workdir="/srv/agents/morty",
    )
    assert "/bin/chown agent-morty:agent-morty /run/claude-agent/env" in rendered
    # And the runtime dir chown follows too.
    assert "/bin/chown agent-morty:agent-morty /run/claude-agent" in rendered


def test_unit_chown_defaults_to_claude():
    """Legacy render keeps chown claude:claude (golden-preserving)."""
    rendered = _render()
    assert "/bin/chown claude:claude /run/claude-agent/env" in rendered
    assert "/bin/chown claude:claude /run/claude-agent" in rendered
