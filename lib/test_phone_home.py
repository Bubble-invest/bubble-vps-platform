"""Static + golden-file tests for the per-tenant phone-home telemetry daemon
(Task C — SPEC-015).

Four surfaces covered (per SPEC-015 §"Test plan > Static tests"):

    1. test_phone_home_no_data_content_in_payload
       Render the bash template and inspect what fields it emits in the
       JSON payload. SPEC-015 §"Critical security property": NO data
       content (no agent message text, no file paths from agent work, no
       secrets, no tokens). Only operational counters + state booleans.
       The test scans the rendered template and asserts:
         a) it does NOT reference any common data-content sources
            (transcripts, message contents, agent file paths)
         b) the JSON-emitting code only references the whitelisted set
            of metric collectors (host/agent/telegram/tailscale/claude_code)

    2. test_phone_home_unsets_token_after_use
       SPEC-008 hard rule extension. Every `${TOKEN}` use in the rendered
       script must be followed by `unset TOKEN`.

    3. test_phone_home_uses_authorization_bearer_header
       SPEC-015 §"Daemon implementation". The token MUST go in an
       `Authorization: Bearer ${TOKEN}` header NOT in the URL as
       `?token=${TOKEN}`. Headers don't show in `ps auxww` output;
       URL query params do.

    4. test_phone_home_systemd_units_render
       Golden-file compare for the .timer and .service unit templates.

Plus belt-and-suspenders extras (orchestration wiring, no plaintext
credential leaks, get_tenant_config discipline) following the
test_telegram_watchdog.py pattern.

Run with: python3.12 -m pytest lib/test_phone_home.py -v
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


# Same set as test_telegram_watchdog.py + test_security_audit.py.
LEAKED_PREFIXES = (
    "8350575119:",   # Telegram bot id (rotated)
    "sk-or-v1-",     # OpenRouter key prefix
    "sk-ant-oat01-", # Anthropic OAuth token prefix
    "tskey-auth-",   # Tailscale auth key prefix
)


# Default render kwargs — matches what phone_home.apply() passes for
# bubble-internal.
_DEFAULT_RENDER_KWARGS = {
    "service_name": "claude-agent-morty.service",
    "decrypted_runtime_path": "/run/claude-agent/env",
    "tenant_name": "bubble-internal",
    "dashboard_url": "http://100.110.56.18:3848/heartbeat",
    "bot_pid_file": "/home/claude/.claude/channels/telegram/bot.pid",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    """Mirror pyinfra's render env + keep_trailing_newline so byte-for-byte
    golden compares match what pyinfra ships to the box."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, **kwargs) -> str:
    return _jinja_env().get_template(template_name).render(**kwargs)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


# ─── 1. Payload contains NO data content ────────────────────────────────────


def test_phone_home_no_data_content_in_payload():
    """SPEC-015 §"Critical security property": the daemon must emit ONLY
    operational metadata. NO message contents, NO file paths from agent
    work, NO transcripts, NO secrets.

    We assert this two ways:

      a) The rendered script must NOT reference common data-content sources
         (transcripts dir, agent message paths, decrypted env file CONTENT
         beyond the single PHONEHOME_TOKEN field, etc.).

      b) The script's JSON-payload-emitting code only references a
         whitelisted set of metric collectors (host/agent/telegram/
         tailscale/claude_code keys per the SPEC-015 telemetry contract).
    """
    rendered = _render("phone-home.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # (a) Forbid references to data-content sources. The agent's transcripts
    # dir is /home/claude/.claude/projects (where every conversation jsonl
    # lives); referencing it from the heartbeat script would be a
    # smoking-gun bug.
    forbidden_paths = [
        "/home/claude/.claude/projects",  # session transcripts
        "/home/claude/.claude/todos",     # agent todo lists
        ".jsonl",                         # session transcript file extension
        "channels/telegram/messages",     # plugin message store
    ]
    for path in forbidden_paths:
        assert path not in rendered, (
            f"phone-home.sh references {path!r} — that's a data-content "
            f"source. Per SPEC-015, the heartbeat must NOT include any "
            f"data content (only operational metadata). Remove the "
            f"reference; if you need a related counter, derive it from "
            f"systemctl/journalctl + emit only an integer."
        )

    # (b) The JSON-key set in the emitted payload must be the exact
    # SPEC-015 whitelist. Find every `printf '"<key>":...'` invocation that
    # writes a TOP-LEVEL field name (matches `printf '"<key>":` with a
    # following bracket = nested object start, OR followed by a literal
    # value).
    top_level_keys = set(re.findall(r"printf '\"([a-z_]+)\"', ", "")) or set()
    # The script uses several patterns; collect them all from the rendered
    # text and union.
    top_level_keys |= set(
        re.findall(r"printf '\"([a-z_]+)\":", rendered)
    )
    # The SPEC-015 telemetry contract top-level keys.
    expected_top = {
        "schema_version",
        "tenant_name",
        "ts_utc",
        "host",
        "agent",
        "telegram",
        "tailscale",
        "claude_code",
        # Sub-object inner keys also match `printf '"<key>":` so we accept
        # them as a superset; the assertion is "no UNEXPECTED top-level
        # keys outside the spec families".
    }
    expected_inner = {
        # host
        "hostname", "uptime_seconds", "disk_pct_used", "memory_pct_used",
        "swap_pct_used", "load_avg_1m",
        # agent
        "service", "is_active", "is_enabled", "restarts_24h",
        # telegram
        "bot_pid_present", "bot_pid_alive",
        "pending_update_count", "last_error_message",
        # tailscale
        "online", "self_ip",
        # claude_code
        "version_installed", "version_latest_npm_cached",
    }
    allowed = expected_top | expected_inner
    unexpected = top_level_keys - allowed
    assert not unexpected, (
        f"phone-home.sh emits unexpected JSON keys {unexpected!r} — these "
        f"are not in the SPEC-015 telemetry contract. If you need a new "
        f"metric, update SPEC-015 + this test allowlist + the dashboard "
        f"renderer in lockstep."
    )


def test_phone_home_no_plaintext_credentials():
    """Belt-and-suspenders: the rendered phone-home script must not contain
    any leaked credential prefix as a literal value (the script reads the
    token at runtime; it must NEVER bake one in)."""
    rendered = _render("phone-home.sh.j2", **_DEFAULT_RENDER_KWARGS)
    for prefix in LEAKED_PREFIXES:
        assert prefix not in rendered, (
            f"Leaked credential prefix {prefix!r} found in rendered "
            f"phone-home.sh — read the token at runtime from "
            f"{_DEFAULT_RENDER_KWARGS['decrypted_runtime_path']}, never "
            f"embed a literal value."
        )


# ─── 2. Token unset after use (SPEC-008 hard rule extension) ────────────────


def test_phone_home_unsets_token_after_use():
    """Every `${TOKEN}` use must be followed by `unset TOKEN` before the
    script exits any path. SPEC-008 hard rule extension.
    """
    rendered = _render("phone-home.sh.j2", **_DEFAULT_RENDER_KWARGS)

    token_uses = re.findall(r"\$\{TOKEN\}", rendered)
    unset_count = len(re.findall(r"\bunset\s+TOKEN\b", rendered))

    assert token_uses, (
        "Sanity: rendered phone-home script doesn't reference ${TOKEN} at "
        "all — broken script (no auth header set on the POST)."
    )
    # Allow >= 1 unset per use (SPEC-008 wants at least one unset after the
    # last use; we accept one unset that covers all uses if they're in a
    # single sequential block).
    assert unset_count >= 1, (
        f"phone-home.sh uses ${{TOKEN}} but never `unset TOKEN`. Per "
        f"SPEC-008 the token must be unset after use so it doesn't linger "
        f"in the shell environment."
    )

    # The LAST `${TOKEN}` reference MUST be followed by `unset TOKEN`.
    last_use = max(m.start() for m in re.finditer(r"\$\{TOKEN\}", rendered))
    last_unset = max(
        (m.start() for m in re.finditer(r"\bunset\s+TOKEN\b", rendered)),
        default=-1,
    )
    assert last_unset > last_use, (
        "The last ${TOKEN} reference in phone-home.sh is NOT followed by "
        "an `unset TOKEN`. Defense-in-depth violation."
    )


# ─── 3. Authorization Bearer header (NOT URL query param) ───────────────────


def test_phone_home_uses_authorization_bearer_header():
    """SPEC-015 §"Daemon implementation": PHONEHOME_TOKEN goes in an
    `Authorization: Bearer ${TOKEN}` header. NOT in a URL query string
    (would show in `ps auxww`, would land in HTTP proxy access logs).
    """
    rendered = _render("phone-home.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # Must use `-H "Authorization: Bearer ${TOKEN}"` (any spacing).
    bearer_pattern = re.compile(
        r'-H\s+["\']Authorization:\s+Bearer\s+\$\{TOKEN\}["\']'
    )
    assert bearer_pattern.search(rendered), (
        "phone-home.sh does NOT include "
        '`-H "Authorization: Bearer ${TOKEN}"` on its curl. Per SPEC-015, '
        "the token must be sent in the Authorization header, NOT in the "
        "URL or in --data."
    )

    # Must NOT use the token in a URL query string. Common bad forms:
    #   http://host/heartbeat?token=${TOKEN}
    #   http://host/heartbeat?auth=${TOKEN}
    bad_url_patterns = [
        r"\?token=\$\{TOKEN\}",
        r"\?auth=\$\{TOKEN\}",
        r"\?phonehome_token=\$\{TOKEN\}",
        r"&token=\$\{TOKEN\}",
    ]
    for pat in bad_url_patterns:
        assert not re.search(pat, rendered), (
            f"phone-home.sh has the token in a URL query param "
            f"({pat!r}) — that leaks via `ps auxww` and proxy access logs. "
            f"Use the Authorization header instead."
        )

    # Must NOT echo the token via --data / --data-urlencode either.
    bad_data_patterns = [
        r"--data[a-z-]*\s+[\"']token=\$\{TOKEN\}",
        r"--data[a-z-]*\s+[\"']PHONEHOME_TOKEN=\$\{TOKEN\}",
    ]
    for pat in bad_data_patterns:
        assert not re.search(pat, rendered), (
            f"phone-home.sh sends the token in a --data form field "
            f"({pat!r}) — that ships the token in the request body, "
            f"which can be logged in places we don't expect. Use the "
            f"Authorization header."
        )


def test_phone_home_uses_token_via_http_to_dashboard():
    """The ONLY place ${TOKEN} appears in a URL/header context must be the
    POST to the dashboard URL we configured. Not Telegram, not a 3rd party.
    """
    rendered = _render("phone-home.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Find every line that uses ${TOKEN} on a curl call.
    token_curl_lines = [
        line for line in rendered.splitlines()
        if "${TOKEN}" in line
    ]
    # Some are header-set lines (-H "Authorization: ..."); the URL line is
    # a separate curl arg. We only assert the token NEVER appears glued
    # onto api.telegram.org or any other unexpected host.
    for line in token_curl_lines:
        assert "api.telegram.org" not in line, (
            f"phone-home.sh sends ${{TOKEN}} to api.telegram.org: {line!r}. "
            f"Wrong destination — PHONEHOME_TOKEN is for the dashboard."
        )


# ─── 4. Golden-file compare for systemd timer + service ─────────────────────


def test_phone_home_systemd_timer_renders():
    rendered = _render("phone-home.timer.j2")
    expected = _golden("phone-home.timer")
    assert rendered == expected, (
        f"phone-home.timer template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_phone_home_systemd_service_renders():
    rendered = _render("phone-home.service.j2")
    expected = _golden("phone-home.service")
    assert rendered == expected, (
        f"phone-home.service template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


# ─── pyinfra task module — orchestration wiring + discipline ────────────────


def test_top_level_deploy_imports_phone_home():
    """deploy.py must import phone_home and call its apply() after
    security_audit.apply(). Order: ... → security_audit → phone_home →
    dashboard → hello.
    """
    deploy_path = REPO_ROOT / "deploy.py"
    source = deploy_path.read_text(encoding="utf-8")
    assert "phone_home" in source, (
        "Top-level deploy.py does not reference phone_home — the Task C "
        "daemon module is never invoked at deploy time."
    )
    assert re.search(r"phone_home\.apply\s*\(", source), (
        "Top-level deploy.py never calls phone_home.apply()."
    )
    audit_pos = source.find("security_audit.apply")
    ph_pos = source.find("phone_home.apply")
    assert audit_pos != -1 and ph_pos != -1, (
        "Both security_audit.apply and phone_home.apply must be present"
    )
    assert audit_pos < ph_pos, (
        f"phone_home.apply (pos {ph_pos}) is invoked BEFORE "
        f"security_audit.apply (pos {audit_pos}). Per SPEC-015 ordering, "
        f"security_audit must come first."
    )


def test_phone_home_pyinfra_module_uses_get_tenant_config():
    """Same architectural invariant: read tenant config via
    lib.host_helpers.get_tenant_config, never parse YAML directly.
    """
    module_path = ACCESS_TASKS_DIR / "phone_home.py"
    source = module_path.read_text(encoding="utf-8")
    assert re.search(
        r"from\s+lib\.host_helpers\s+import[^\n]*\bget_tenant_config\b",
        source,
    ), (
        "phone_home.py must import get_tenant_config from "
        "lib.host_helpers — direct YAML parsing in pyinfra tasks bypasses "
        "the SPEC-003 host-data exposure policy."
    )


def test_phone_home_pyinfra_module_drops_systemd_units():
    """Static check the module installs both systemd units."""
    module_path = ACCESS_TASKS_DIR / "phone_home.py"
    source = module_path.read_text(encoding="utf-8")
    assert "/etc/systemd/system/phone-home.timer" in source
    assert "/etc/systemd/system/phone-home.service" in source
    assert re.search(r"files\.template\s*\(", source)


def test_phone_home_required_keys_includes_phonehome_token():
    """tenant.yaml's secrets.required_keys must include PHONEHOME_TOKEN.
    This is the gating mechanism — pyinfra deploy will fail at the secrets
    validation step until the operator pastes the token via
    operator-set-secret.sh.
    """
    # Resolve the data repo the same way inventory.py does.
    import os
    raw = os.environ.get("BUBBLE_DATA_REPO")
    if raw:
        data_repo = Path(raw).expanduser().resolve()
    else:
        data_repo = (REPO_ROOT / ".." / "bubble-vps-data").resolve()

    tenant_yaml = data_repo / "tenants" / "bubble-internal" / "tenant.yaml"
    if not tenant_yaml.is_file():
        pytest.skip(
            f"Skipping — bubble-vps-data not available at {data_repo} "
            f"(test runs only in the operator's full workspace)."
        )

    import yaml
    with tenant_yaml.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    required = cfg.get("secrets", {}).get("required_keys", []) or []
    assert "PHONEHOME_TOKEN" in required, (
        "tenant.yaml secrets.required_keys is missing PHONEHOME_TOKEN. "
        "Per SPEC-015, this is the gate that forces the operator to paste "
        "the token before the dashboard can authenticate phone-home POSTs."
    )


# ─── Set -uo pipefail (NOT -e) — same rationale as the watchdog ─────────────


def test_phone_home_script_uses_set_uo_pipefail_not_e():
    """Each metric collection is independent; one failure shouldn't abort
    the whole heartbeat (we'd rather emit a partial JSON than nothing)."""
    rendered = _render("phone-home.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "set -uo pipefail" in rendered, (
        "phone-home.sh must `set -uo pipefail` (NOT -e); each metric "
        "collection is independent and one failure shouldn't abort "
        "subsequent ones."
    )


# ─── Sudo-escalation static guard (regression: 2026-05-31 deploy blocker) ─────
#
# The deploy connects AS claude (tenant ssh_user: claude). phone_home's apply()
# writes BOTH root-owned files (/etc/systemd/system/phone-home.{timer,service})
# AND claude-owned files (/home/claude/scripts/{,phone-home.sh}), then runs
# root-only systemctl commands (daemon-reload, enable/start, restart the timer).
# Each must escalate correctly or pyinfra dies with `[Errno 13] Permission
# denied` → `No hosts remaining!`. These AST tests pin the escalation kwargs so
# a refactor can't silently drop them. Mirror of TestSecurityAuditSudoEscalation
# + TestDashboardSudoEscalation + TestCloudWikiSyncSudoEscalation — the same gap
# fixed in commit 8f6fbec (agent + watchdog), cloud_wiki_sync, dashboard, and
# security_audit.
#
# RULE: root target (/etc, /usr, /var/log, /var/lib) → `_sudo=True` ALONE (NO
#       _sudo_user — writing as claude to /etc is still Permission denied);
#       claude target (/home/claude) → `_sudo=True, _sudo_user="claude"`;
#       root-only command (systemctl/daemon-reload) → `_sudo=True`.


def _ph_assign_env(tree):
    import ast

    env: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(n.value)
    return env


def _ph_str_fragments(node, env, _depth=0):
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
                out |= _ph_str_fragments(v.value, env, _depth + 1)
    elif isinstance(node, ast.Name):
        for val in env.get(node.id, []):
            out |= _ph_str_fragments(val, env, _depth + 1)
    elif isinstance(node, ast.BinOp):
        out |= _ph_str_fragments(node.left, env, _depth + 1)
        out |= _ph_str_fragments(node.right, env, _depth + 1)
    return out


def _ph_has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def _ph_kw_is_claude(node, name):
    import ast

    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value == "claude"
    return False


def _ph_classify(fragments):
    # Root territory: /etc, /usr (systemd units), /var/log, /var/lib for
    # completeness. Only /home/claude paths are claude-escalated.
    if any(
        f.startswith("/etc/")
        or f.startswith("/usr/")
        or f.startswith("/var/log")
        or f.startswith("/var/lib")
        for f in fragments
    ):
        return "root"
    if any("/home/claude" in f for f in fragments):
        return "claude"
    return "unknown"


def _ph_ops(tree):
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


def _ph_command_strings(node):
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


class TestPhoneHomeSudoEscalation:
    """phone_home.py escalates every root write / root command and uses
    _sudo_user=claude for the /home/claude files. AST guard for the
    2026-05-31 deploy blocker — same gap fixed in the agent + watchdog tasks
    (commit 8f6fbec), cloud_wiki_sync.py, dashboard.py, and security_audit.py."""

    def _tree(self):
        import ast

        path = ACCESS_TASKS_DIR / "phone_home.py"
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_root_writes_have_sudo_no_sudo_user(self):
        tree = self._tree()
        env = _ph_assign_env(tree)
        found_root = 0
        for qual, node in _ph_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _ph_str_fragments(kw.value, env)
            if _ph_classify(frags) == "root":
                found_root += 1
                assert _ph_has_kw(node, "_sudo"), (
                    f"phone_home: {qual} writing root path {sorted(frags)} "
                    f"is MISSING _sudo=True (deploy connects AS claude)."
                )
                assert not _ph_has_kw(node, "_sudo_user"), (
                    f"phone_home: {qual} writing root path {sorted(frags)} "
                    f"must NOT set _sudo_user (escalate to root, not claude — "
                    f"writing as claude to /etc is still Permission denied)."
                )
        # timer (/etc) + service (/etc) = two root-territory writes.
        assert found_root >= 2, (
            f"Expected >=2 root-owned writes in phone_home.py (the "
            f"/etc/systemd/system timer + service units), classifier found "
            f"{found_root}."
        )

    def test_home_claude_writes_have_sudo_user_claude(self):
        tree = self._tree()
        env = _ph_assign_env(tree)
        found_claude = 0
        for qual, node in _ph_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _ph_str_fragments(kw.value, env)
            if _ph_classify(frags) == "claude":
                found_claude += 1
                assert _ph_has_kw(node, "_sudo"), (
                    f"phone_home: {qual} writing claude path "
                    f"{sorted(frags)} is MISSING _sudo=True."
                )
                assert _ph_kw_is_claude(node, "_sudo_user"), (
                    f"phone_home: {qual} writing claude path "
                    f"{sorted(frags)} must set _sudo_user=\"claude\"."
                )
        # /home/claude/scripts/ dir + phone-home.sh template = two writes.
        assert found_claude >= 2, (
            f"Expected >=2 claude-owned /home/claude writes (scripts dir + "
            f"phone-home.sh), classifier found {found_claude}."
        )

    def test_systemctl_shell_commands_have_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _ph_ops(tree):
            if qual != "server.shell":
                continue
            joined = "\n".join(_ph_command_strings(node))
            if "systemctl " in joined or "journalctl " in joined:
                seen += 1
                assert _ph_has_kw(node, "_sudo"), (
                    f"phone_home: server.shell running {joined!r} is "
                    f"MISSING _sudo=True — systemctl is root-only."
                )
                assert not _ph_kw_is_claude(node, "_sudo_user"), (
                    f"phone_home: server.shell running {joined!r} must "
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
        for qual, node in _ph_ops(tree):
            if qual != "systemd.service":
                continue
            seen += 1
            assert _ph_has_kw(node, "_sudo"), (
                "phone_home: systemd.service (enable/start the timer) "
                "is MISSING _sudo=True — root-only operation."
            )
            assert not _ph_kw_is_claude(node, "_sudo_user"), (
                "phone_home: systemd.service must NOT set _sudo_user=claude — "
                "systemctl enable/start is root-only."
            )
        assert seen >= 1, (
            "Expected a systemd.service op (enable+start the timer) in "
            "phone_home.py."
        )
