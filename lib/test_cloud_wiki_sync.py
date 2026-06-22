"""Static + golden-file tests for the cloud-side wiki sync timer
(Phase 5b — SPEC-020).

Covers four surfaces:

    1. Plaintext-leak guards on the rendered sync bash script:
       - Known leaked-credential prefixes MUST NOT appear in the rendered
         template. The script reads the token at runtime from the tmpfs
         env file — it must never bake a credential into the template.

    2. SPEC-008 hard rule extension to the sync script:
       - Every `${TOKEN}` use is followed by `unset TOKEN` (or by a
         cleanup_token() call that includes `unset GITHUB_TOKEN`).
       - The bot token (TELEGRAM_BOT_TOKEN) used in the conflict-alert
         path is also unset after use.

    3. SPEC-020 hard rule (CRITICAL): no token-in-URL pattern.
       - Asserts the rendered script never contains
         `https://x-access-token:` or `https://*:*@github.com` patterns
         (those would persist in `.git/config` and process listings).
       - The credential-helper pattern (GIT_ASKPASS) is the documented
         secure way for fine-grained PAT HTTPS auth.

    4. Golden-file compare for .timer + .service systemd units (mirrors
       test_telegram_watchdog.py + test_phone_home.py shape).

    5. pyinfra task module structural checks:
       - Initial-clone gate uses `test -d ... || (...)` shape.
       - Module imports get_tenant_config (no direct YAML parsing).
       - deploy.py wires cloud_wiki_sync.apply() in the correct slot.

Run with: python3.12 -m pytest lib/test_cloud_wiki_sync.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyinfra" / "templates"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "access"
ACCESS_TASKS_DIR = REPO_ROOT / "pyinfra" / "tasks" / "access"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Literal credential prefixes that MUST NOT leak into the rendered sync
# script. Same set as test_telegram_watchdog.py + GitHub PAT prefixes
# (the PAT we just shipped uses `github_pat_` for fine-grained tokens
# and `ghp_` for classic — guard both).
LEAKED_PREFIXES = (
    "8350575119:",      # Telegram bot id (rotated)
    "sk-or-v1-",        # OpenRouter key prefix
    "sk-ant-oat01-",    # Anthropic OAuth token prefix
    "tskey-auth-",      # Tailscale auth key prefix
    "github_pat_",      # GitHub fine-grained PAT prefix
    "ghp_",             # GitHub classic PAT prefix
)


# Default render kwargs — matches what cloud_wiki_sync.apply() passes for
# bubble-internal. Keep these in sync if the module's defaults change.
_DEFAULT_RENDER_KWARGS = {
    "wiki_dir": "/home/claude/.claude/agent-memory/shared-wiki",
    "wiki_remote_url": "https://github.com/example-org/bubble-shared-wiki",
    "lock_dir": "/run/cloud-wiki-sync/lock",
    "credential_helper_path": "/home/claude/scripts/git-credential-helper.sh",
    "decrypted_runtime_path": "/run/claude-agent/env",
    "operator_telegram_user_id": "100000001",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    """Mirror pyinfra's render env (default Environment + keep_trailing_newline)
    so byte-for-byte golden compares match what pyinfra ships to the box."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, **kwargs) -> str:
    return _jinja_env().get_template(template_name).render(**kwargs)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


# ─── 1. Sync script — no plaintext credentials in template ──────────────────


def test_sync_script_no_plaintext_credential_in_template():
    """Render the bash template, grep for any known leaked-credential prefix.
    The script reads the token at runtime from a tmpfs env file — it must
    NEVER bake a credential into the template itself.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    for prefix in LEAKED_PREFIXES:
        assert prefix not in rendered, (
            f"Leaked credential prefix {prefix!r} found in rendered "
            f"cloud-wiki-sync.sh — the script must read the token at "
            f"runtime from {_DEFAULT_RENDER_KWARGS['decrypted_runtime_path']}, "
            f"never embed it as a template literal."
        )


# ─── 2. SPEC-008 — every TOKEN use followed by unset ────────────────────────


def test_sync_script_unsets_token_after_use():
    """SPEC-008 hard rule: GITHUB_TOKEN and TELEGRAM_BOT_TOKEN must both be
    unset after use. The script either calls `unset TOKEN`/`unset GITHUB_TOKEN`/
    `unset BOT_TOKEN` directly OR calls a cleanup function (cleanup_token)
    that does so. Verify by counting + presence.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # Sanity: the script does reference these env vars
    assert "GITHUB_TOKEN" in rendered, (
        "Sync script doesn't reference GITHUB_TOKEN at all — broken script."
    )
    assert "TELEGRAM_BOT_TOKEN" in rendered, (
        "Sync script doesn't reference TELEGRAM_BOT_TOKEN at all — the "
        "conflict-alert path can't notify on rebase failure."
    )

    # GITHUB_TOKEN must be unset somewhere (directly or via cleanup function)
    has_github_unset = (
        "unset GITHUB_TOKEN" in rendered
        or re.search(r"unset\s+TOKEN\b", rendered) is not None
    )
    assert has_github_unset, (
        "Sync script never `unset`s GITHUB_TOKEN (or its TOKEN alias). "
        "SPEC-008 requires the token to be cleared so it doesn't linger."
    )

    # Bot token (used for the conflict alert) must also be unset
    has_bot_unset = "unset BOT_TOKEN" in rendered
    assert has_bot_unset, (
        "Sync script never `unset`s BOT_TOKEN after the conflict-alert "
        "curl. SPEC-008 requires the token to be cleared so it doesn't "
        "linger in the shell environment."
    )


# ─── 3. SPEC-020 hard rule — token NEVER in URL ─────────────────────────────


def test_sync_script_uses_credential_helper_not_url_token():
    """CRITICAL SPEC-020 rule. The script MUST NOT contain any pattern that
    embeds the GitHub token in a URL. That would persist the token in
    .git/config (after clone) and would expose it in `ps auxww` while git
    is running.

    Forbidden patterns:
      - `https://x-access-token:`  (the canonical bad pattern from GitHub docs)
      - `https://${TOKEN}@`        (variable-in-URL)
      - `https://$TOKEN@`          (unbraced variable-in-URL)
      - `https://${GITHUB_TOKEN}@`
      - `https://$GITHUB_TOKEN@`
      - generic `https://*:*@github.com` regex (catches any user:password URL)

    The credential-helper pattern (GIT_ASKPASS=<helper> with helper reading
    GITHUB_TOKEN from env) is the documented secure way for fine-grained PAT
    HTTPS auth.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)

    forbidden_substrings = [
        "https://x-access-token:",
        "https://${TOKEN}@",
        "https://$TOKEN@",
        "https://${GITHUB_TOKEN}@",
        "https://$GITHUB_TOKEN@",
        # GitHub's other token-in-URL patterns
        "https://oauth2:",
        "://x-access-token:",
    ]
    for bad in forbidden_substrings:
        assert bad not in rendered, (
            f"FORBIDDEN token-in-URL pattern {bad!r} found in rendered "
            f"cloud-wiki-sync.sh. Per SPEC-020, the credential-helper "
            f"pattern (GIT_ASKPASS) is the only allowed token-passing "
            f"mechanism — URL embedding persists the token in .git/config."
        )

    # Generic regex: https://anything:anything@github.com — catches any
    # user:password URL form regardless of variable name used.
    bad_url_re = re.compile(r"https://[^/\s]+:[^/\s@]+@github\.com")
    matches = bad_url_re.findall(rendered)
    assert not matches, (
        f"FORBIDDEN https://user:password@github.com URL form found in "
        f"rendered cloud-wiki-sync.sh: {matches!r}. Use the credential "
        f"helper (GIT_ASKPASS) instead — URL embedding leaks via "
        f"`ps auxww` AND persists in .git/config after clone."
    )

    # POSITIVE assertion: the script DOES use the credential helper pattern.
    assert "GIT_ASKPASS" in rendered, (
        "Sync script never sets GIT_ASKPASS — the credential-helper "
        "pattern isn't being used. Without it, git either prompts (will "
        "hang in a non-TTY systemd context) or falls back to URL-embedded "
        "auth (which is forbidden)."
    )


def test_pyinfra_module_initial_clone_no_token_in_url():
    """Same SPEC-020 rule, applied to the pyinfra task module's initial-clone
    server.shell command. The module's `git clone https://github.com/...`
    string must NOT embed the token in the URL.
    """
    module_path = ACCESS_TASKS_DIR / "cloud_wiki_sync.py"
    source = module_path.read_text(encoding="utf-8")

    forbidden_substrings = [
        "https://x-access-token:",
        "https://${TOKEN}@",
        "https://$TOKEN@",
        "https://${GITHUB_TOKEN}@",
        "https://$GITHUB_TOKEN@",
        "https://oauth2:",
    ]
    for bad in forbidden_substrings:
        assert bad not in source, (
            f"FORBIDDEN token-in-URL pattern {bad!r} found in "
            f"cloud_wiki_sync.py source. Use GIT_ASKPASS=<helper> to pass "
            f"the token via the credential protocol."
        )

    bad_url_re = re.compile(r"https://[^/\s\"']+:[^/\s@\"']+@github\.com")
    matches = bad_url_re.findall(source)
    assert not matches, (
        f"FORBIDDEN https://user:password@github.com URL form in "
        f"cloud_wiki_sync.py: {matches!r}"
    )


# ─── 4. Credential helper template ──────────────────────────────────────────


def test_credential_helper_uses_x_access_token_username():
    """The GitHub fine-grained-PAT HTTPS auth pattern uses x-access-token as
    the username and the token as the password. Verify the helper emits
    that pair per git's credential-helper protocol.
    """
    rendered = _render("git-credential-helper.sh.j2")
    assert "username=x-access-token" in rendered, (
        "Git credential helper must emit `username=x-access-token` per "
        "GitHub's documented fine-grained-PAT HTTPS auth pattern."
    )
    assert 'password=${GITHUB_TOKEN' in rendered or "password=$GITHUB_TOKEN" in rendered, (
        "Git credential helper must emit `password=${GITHUB_TOKEN}` so "
        "the token comes from the calling process's env at request time."
    )


def test_credential_helper_no_plaintext_credential_in_template():
    """Same plaintext-leak guard as the sync script — applied to the helper."""
    rendered = _render("git-credential-helper.sh.j2")
    for prefix in LEAKED_PREFIXES:
        assert prefix not in rendered, (
            f"Leaked credential prefix {prefix!r} in git-credential-helper.sh"
        )


# ─── 5. Golden-file compare for systemd timer + service ─────────────────────


def test_systemd_timer_renders():
    """Render cloud-wiki-sync.timer.j2; compare to the committed golden
    (which is what pyinfra will write to /etc/systemd/system/ on the box).
    """
    rendered = _render("cloud-wiki-sync.timer.j2")
    expected = _golden("cloud-wiki-sync.timer")
    assert rendered == expected, (
        f"cloud-wiki-sync.timer template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_systemd_service_renders():
    """Render cloud-wiki-sync.service.j2; compare to the committed golden."""
    rendered = _render("cloud-wiki-sync.service.j2")
    expected = _golden("cloud-wiki-sync.service")
    assert rendered == expected, (
        f"cloud-wiki-sync.service template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_systemd_service_runs_as_claude_not_root():
    """Per SPEC-020 §"systemd timer + service": User=claude (NOT root).
    The wiki dir is owned by claude, no privileged operations are needed.
    Running as root would risk creating root-owned files in claude's
    homedir, which would corrupt subsequent agent operations.
    """
    rendered = _render("cloud-wiki-sync.service.j2")
    assert re.search(r"^User=claude\b", rendered, re.MULTILINE), (
        "cloud-wiki-sync.service must declare User=claude — running as "
        "root would create root-owned files in claude's wiki dir, "
        "corrupting subsequent agent operations."
    )
    assert "User=root" not in rendered, (
        "cloud-wiki-sync.service must NOT run as root."
    )


# ─── 6. pyinfra task module structural checks ──────────────────────────────


def test_pyinfra_module_uses_initial_clone_guard():
    """Per SPEC-020, the initial clone must be gated by `test -d ... || (...)`
    so re-runs after first successful clone are no-ops.
    """
    module_path = ACCESS_TASKS_DIR / "cloud_wiki_sync.py"
    source = module_path.read_text(encoding="utf-8")
    # Look for the `test -d ... /.git || (` shape
    assert re.search(r"test\s+-d\s+\S+/\.git\s*\|\|\s*\(", source), (
        "cloud_wiki_sync.py must guard the initial clone with "
        "`test -d <wiki>/.git || (...)` — without this, every deploy "
        "would re-clone and corrupt the local working tree."
    )


def test_pyinfra_module_uses_get_tenant_config():
    """Architectural invariant (same as secrets + agent + watchdog layers):
    read tenant config via lib.host_helpers.get_tenant_config — never parse
    YAML directly in pyinfra task modules.
    """
    module_path = ACCESS_TASKS_DIR / "cloud_wiki_sync.py"
    source = module_path.read_text(encoding="utf-8")
    assert re.search(
        r"from\s+lib\.host_helpers\s+import[^\n]*\bget_tenant_config\b",
        source,
    ), (
        "cloud_wiki_sync.py must import get_tenant_config from "
        "lib.host_helpers — direct YAML parsing in pyinfra tasks bypasses "
        "the SPEC-003 host-data exposure policy."
    )


def test_pyinfra_module_drops_credential_helper():
    """Sanity: the module must drop the credential helper script BEFORE the
    initial clone runs (otherwise GIT_ASKPASS=<helper> can't resolve).
    """
    module_path = ACCESS_TASKS_DIR / "cloud_wiki_sync.py"
    source = module_path.read_text(encoding="utf-8")
    # Must reference the helper template + path
    assert "git-credential-helper.sh" in source, (
        "cloud_wiki_sync.py must drop the git-credential-helper.sh script."
    )
    # Helper template file itself must exist
    helper_template = TEMPLATES_DIR / "git-credential-helper.sh.j2"
    assert helper_template.is_file(), (
        f"git-credential-helper.sh.j2 missing at {helper_template}"
    )


# ─── 7. deploy.py orchestration wiring ──────────────────────────────────────


def test_top_level_deploy_imports_cloud_wiki_sync():
    """deploy.py must import the cloud_wiki_sync task module and call its
    apply() AFTER phone_home.apply(). Order per SPEC-020 + the task brief:
    hardening → secrets → agent → tailscale → telegram_watchdog →
    security_audit → phone_home → cloud_wiki_sync → dashboard → hello.
    """
    deploy_path = REPO_ROOT / "deploy.py"
    source = deploy_path.read_text(encoding="utf-8")
    assert "cloud_wiki_sync" in source, (
        "Top-level deploy.py does not reference cloud_wiki_sync — the "
        "Phase 5b module is never invoked at deploy time."
    )
    assert re.search(r"cloud_wiki_sync\.apply\s*\(", source), (
        "Top-level deploy.py never calls cloud_wiki_sync.apply()."
    )
    # Must come AFTER phone_home.apply()
    phone_home_pos = source.find("phone_home.apply")
    cloud_sync_pos = source.find("cloud_wiki_sync.apply")
    assert phone_home_pos != -1 and cloud_sync_pos != -1, (
        "Both phone_home.apply and cloud_wiki_sync.apply must be present"
    )
    assert phone_home_pos < cloud_sync_pos, (
        f"cloud_wiki_sync.apply (pos {cloud_sync_pos}) is invoked BEFORE "
        f"phone_home.apply (pos {phone_home_pos}). Per task ordering, "
        f"phone_home must come first."
    )


# ─── 8. Sync script — uses set -uo pipefail (NOT -e) ───────────────────────


def test_sync_script_uses_set_uo_pipefail_not_e():
    """SPEC-020 +  Step 7a's lesson: `set -uo pipefail` (NOT -e — we want
    to handle each step explicitly so cleanup paths always run). With -e,
    a subshell exit code may not propagate, leaving the lock + token
    state inconsistent.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "set -uo pipefail" in rendered, (
        "Sync script must `set -uo pipefail` (NOT -e); see SPEC-013 + "
        "SPEC-020 rationale (subshell exit codes don't propagate)."
    )
    # Defensively: the script should NOT have `set -e` (or `set -euo`)
    assert not re.search(r"^\s*set\s+-e", rendered, re.MULTILINE), (
        "Sync script should NOT use `set -e` — handle exit codes "
        "explicitly so cleanup runs."
    )


# ─── 9. Sync script — has lock + conflict handling ─────────────────────────


def test_sync_script_has_single_instance_lock():
    """Per SPEC-020, the script must use a single-instance lock at the
    configured lock_dir to prevent overlapping syncs (slow network +
    big push).
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert _DEFAULT_RENDER_KWARGS["lock_dir"] in rendered, (
        f"Sync script must reference lock_dir "
        f"({_DEFAULT_RENDER_KWARGS['lock_dir']})"
    )
    # mkdir-as-lock pattern (atomic on POSIX)
    assert "mkdir" in rendered and "LOCK_DIR" in rendered, (
        "Sync script must use mkdir-as-lock pattern (atomic on POSIX)"
    )


def test_sync_script_aborts_rebase_on_conflict():
    """Per SPEC-020 conflict policy: on `git pull --rebase` failure, abort
    the rebase + restore the autostash + Telegram alert + exit non-zero.
    Do NOT auto-resolve.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "git rebase --abort" in rendered, (
        "Sync script must `git rebase --abort` on conflict — leaving the "
        "tree in a half-rebased state corrupts the next tick."
    )
    assert "git stash pop" in rendered, (
        "Sync script must `git stash pop` after rebase abort to restore "
        "the autostash."
    )
    assert "api.telegram.org" in rendered, (
        "Sync script must Telegram-alert on rebase conflict (per SPEC-020 "
        "+ Mac-side wiki-github-sync precedent)."
    )


# ─── 9. Pipefail correctness — distinguish transient vs real conflict ─────


def test_sync_script_captures_pipestatus_for_git_pull():
    """SPEC-020 §"Conflict-vs-transient-failure" (added 2026-05-12 after a
    false-positive incident): when `git pull --rebase` is piped through
    `logger`, `$?` after the pipe reflects the if-conditional, NOT the
    pipe's stages. To get git's true exit code we must use
    ${PIPESTATUS[0]} captured IMMEDIATELY after the pipe — any intervening
    command would clobber it.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # The bug: `if ! git pull ... | logger; then rc=$?` returns logger's rc,
    # not git's. The fix MUST run the pipe unconditionally, then read
    # PIPESTATUS, then branch on rc.
    assert "PIPESTATUS[0]" in rendered, (
        "Sync script must capture git pull's true exit code via "
        "${PIPESTATUS[0]} — using $? after the pipe returns the wrong "
        "code under pipefail + if-not (false-positive 2026-05-12 root cause)."
    )
    # And the script must NOT use the old broken pattern.
    assert "if ! git pull --rebase --autostash 2>&1 | logger" not in rendered, (
        "Sync script still uses the broken `if ! git pull ... | logger` "
        "pattern. Replace with unconditional pipe + PIPESTATUS check."
    )


def test_sync_script_distinguishes_transient_from_real_conflict():
    """SPEC-020: a transient `git pull` failure (network, auth, GitHub 5xx)
    must NOT fire a Telegram conflict alert — the next 30-min tick retries
    naturally. Only a real rebase that paused mid-flight (`.git/rebase-merge/`
    or `.git/rebase-apply/` present after the failure) is a conflict.

    The old code unconditionally claimed conflict and alerted on EVERY git
    pull failure, including network blips. False positive observed
    2026-05-12T00:38:56Z: "Connection reset by peer" → fake conflict alert.
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Must check for a paused rebase as the conflict signal.
    assert ".git/rebase-merge" in rendered, (
        "Sync script must check for .git/rebase-merge/ (or rebase-apply/) "
        "as the conflict signal — its presence is the only reliable "
        "way to distinguish a real paused rebase from a pre-rebase "
        "transient failure (fetch error, auth glitch, network blip)."
    )
    assert ".git/rebase-apply" in rendered, (
        "Sync script must also check for .git/rebase-apply/ — the am-style "
        "backend used by some git configs leaves this dir instead of "
        "rebase-merge/."
    )


def test_sync_script_transient_failure_does_not_alert():
    """SPEC-020 + this incident's fix: on transient git pull failure, log a
    WARN and exit 0 (so systemd marks the unit as cleanly succeeded — the
    next timer tick will retry). Do NOT alert; do NOT exit non-zero (which
    would mark the unit as Failed and could trigger downstream watchdogs).
    """
    rendered = _render("cloud-wiki-sync.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # The transient branch must log a WARN (so it's still visible in journal)
    # and exit 0 (not 2 or any other non-zero — that's reserved for real
    # conflict).
    assert "WARN: git pull failed" in rendered, (
        "Sync script's transient-failure branch must log a WARN line so the "
        "incident is visible in journalctl without being an alert."
    )
    # The exit 0 after the transient branch should be present.
    # We look for the specific pattern "Skipping this tick" + "exit 0" near
    # each other.
    assert (
        "Skipping this tick" in rendered
        and rendered.count("exit 0") >= 1
    ), (
        "Sync script's transient-failure branch must exit 0 (not 2) so the "
        "systemd unit doesn't enter Failed state on every network blip."
    )


# ─── 10. Sudo-escalation static guard (regression: 2026-05-31 deploy blocker) ─
#
# The deploy connects AS claude (tenant ssh_user: claude). cloud_wiki_sync's
# apply() writes BOTH root-owned files (/etc/systemd/system/cloud-wiki-sync.
# {timer,service}) AND claude-owned files (/home/claude/scripts/*), and runs
# root-only systemctl commands (daemon-reload, enable/start/restart the timer).
# Each must escalate correctly or pyinfra dies with `[Errno 13] Permission
# denied` → `No hosts remaining!`. These AST tests pin the escalation kwargs so
# a refactor can't silently drop them. Mirror of TestWatchdogSudoEscalation in
# lib/test_telegram_watchdog.py + TestAgentLayerSudoEscalation.
#
# RULE: root target (/etc) → `_sudo=True` ALONE (NO _sudo_user — writing as
#       claude to /etc is still Permission denied); claude target
#       (/home/claude) → `_sudo=True, _sudo_user="claude"`; root-only command
#       (systemctl/daemon-reload) → `_sudo=True`.


def _cws_assign_env(tree):
    import ast

    env: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(n.value)
    return env


def _cws_str_fragments(node, env, _depth=0):
    import ast

    out: set = set()
    if _depth > 12:
        return out
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.add(node.value)
    elif isinstance(node, ast.JoinedStr):
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.add(v.value)
            elif isinstance(v, ast.FormattedValue):
                out |= _cws_str_fragments(v.value, env, _depth + 1)
    elif isinstance(node, ast.Name):
        for val in env.get(node.id, []):
            out |= _cws_str_fragments(val, env, _depth + 1)
    elif isinstance(node, ast.BinOp):
        out |= _cws_str_fragments(node.left, env, _depth + 1)
        out |= _cws_str_fragments(node.right, env, _depth + 1)
    return out


def _cws_has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def _cws_kw_is_claude(node, name):
    import ast

    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value == "claude"
    return False


def _cws_classify(fragments):
    if any(f.startswith("/etc/") or f.startswith("/usr/") for f in fragments):
        return "root"
    if any("/home/claude" in f for f in fragments):
        return "claude"
    return "unknown"


def _cws_ops(tree):
    import ast

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            qual = f"{f.value.id}.{f.attr}"
            if qual in (
                "files.template",
                "files.put",
                "files.directory",
                "files.file",
                "server.shell",
                "systemd.service",
            ):
                yield qual, node


def _cws_command_strings(node):
    import ast

    cmds = []
    for kw in node.keywords:
        if kw.arg != "commands":
            continue
        if isinstance(kw.value, ast.List):
            for elt in kw.value.elts:
                parts = []
                for sub in ast.walk(elt):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        parts.append(sub.value)
                if parts:
                    cmds.append(" ".join(parts))
    return cmds


class TestCloudWikiSyncSudoEscalation:
    """cloud_wiki_sync.py escalates every root write / root command and uses
    _sudo_user=claude for the /home/claude scripts. AST guard for the
    2026-05-31 deploy blocker (same gap that was fixed in the agent + watchdog
    tasks in commit 8f6fbec)."""

    def _tree(self):
        import ast

        path = ACCESS_TASKS_DIR / "cloud_wiki_sync.py"
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_etc_writes_have_sudo_no_sudo_user(self):
        tree = self._tree()
        env = _cws_assign_env(tree)
        found_root = 0
        for qual, node in _cws_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _cws_str_fragments(kw.value, env)
            if _cws_classify(frags) == "root":
                found_root += 1
                assert _cws_has_kw(node, "_sudo"), (
                    f"cloud_wiki_sync: {qual} writing root path {sorted(frags)} "
                    f"is MISSING _sudo=True (deploy connects AS claude)."
                )
                assert not _cws_has_kw(node, "_sudo_user"), (
                    f"cloud_wiki_sync: {qual} writing root path {sorted(frags)} "
                    f"must NOT set _sudo_user (escalate to root, not claude — "
                    f"writing as claude to /etc is still Permission denied)."
                )
        # timer + service = two /etc writes expected.
        assert found_root >= 2, (
            f"Expected >=2 root-owned /etc writes in cloud_wiki_sync.py "
            f"(timer + service), classifier found {found_root}."
        )

    def test_home_claude_writes_have_sudo_user_claude(self):
        tree = self._tree()
        env = _cws_assign_env(tree)
        found_claude = 0
        for qual, node in _cws_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _cws_str_fragments(kw.value, env)
            if _cws_classify(frags) == "claude":
                found_claude += 1
                assert _cws_has_kw(node, "_sudo"), (
                    f"cloud_wiki_sync: {qual} writing claude path "
                    f"{sorted(frags)} is MISSING _sudo=True."
                )
                assert _cws_kw_is_claude(node, "_sudo_user"), (
                    f"cloud_wiki_sync: {qual} writing claude path "
                    f"{sorted(frags)} must set _sudo_user=\"claude\"."
                )
        # /home/claude/scripts/ dir + credential helper .sh + sync .sh = three
        # claude writes.
        assert found_claude >= 3, (
            f"Expected >=3 claude-owned /home/claude writes (scripts dir + "
            f"credential helper + sync script), classifier found {found_claude}."
        )

    def test_systemctl_shell_commands_have_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _cws_ops(tree):
            if qual != "server.shell":
                continue
            joined = "\n".join(_cws_command_strings(node))
            if "systemctl " in joined or "journalctl " in joined:
                seen += 1
                assert _cws_has_kw(node, "_sudo"), (
                    f"cloud_wiki_sync: server.shell running {joined!r} is "
                    f"MISSING _sudo=True — systemctl is root-only."
                )
                # A root systemctl command must NOT be escalated to claude.
                assert not _cws_kw_is_claude(node, "_sudo_user"), (
                    f"cloud_wiki_sync: server.shell running {joined!r} must "
                    f"NOT set _sudo_user=claude — systemctl is root-only."
                )
        # daemon-reload + restart timer = two root systemctl shells expected.
        assert seen >= 2, (
            f"Expected >=2 root systemctl server.shell ops (daemon-reload + "
            f"restart timer), classifier found {seen}."
        )

    def test_systemd_service_op_has_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _cws_ops(tree):
            if qual != "systemd.service":
                continue
            seen += 1
            assert _cws_has_kw(node, "_sudo"), (
                "cloud_wiki_sync: systemd.service (enable/start the timer) "
                "is MISSING _sudo=True — root-only operation."
            )
        assert seen >= 1, (
            "Expected a systemd.service op (enable+start the timer) in "
            "cloud_wiki_sync.py."
        )

    def test_initial_clone_shell_runs_as_claude(self):
        """The initial-clone server.shell writes to /home/claude/.claude/... as
        the claude user (git clone the wiki into claude's homedir). It runs NO
        systemctl, so it must escalate to claude (`_sudo=True,
        _sudo_user="claude"`), NOT bare root (a root clone would leave
        root-owned files in claude's homedir, breaking the agent)."""
        tree = self._tree()
        clone_nodes = []
        for qual, node in _cws_ops(tree):
            if qual != "server.shell":
                continue
            joined = "\n".join(_cws_command_strings(node))
            if "git clone" in joined:
                clone_nodes.append(node)
        assert len(clone_nodes) == 1, (
            f"Expected exactly one initial-clone server.shell in "
            f"cloud_wiki_sync.py, found {len(clone_nodes)}."
        )
        node = clone_nodes[0]
        assert _cws_has_kw(node, "_sudo"), (
            "cloud_wiki_sync: initial-clone server.shell is MISSING _sudo=True."
        )
        assert _cws_kw_is_claude(node, "_sudo_user"), (
            "cloud_wiki_sync: initial-clone server.shell must set "
            "_sudo_user=\"claude\" so the clone lands in claude's homedir "
            "owned by claude (NOT root)."
        )
