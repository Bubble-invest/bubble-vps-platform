"""Static + golden-file tests for the central Bubble VPS dashboard
(Task C — SPEC-015).

Four surfaces covered (per SPEC-015 §"Test plan > Static tests"):

    1. test_dashboard_binds_to_tailscale_ip_via_envfile
       The systemd unit MUST derive BIND_ADDR from `tailscale ip -4` via
       an ExecStartPre that writes /run/bubble-dashboard.env, NOT
       hardcoded 0.0.0.0 or a hardcoded literal IP. Defense in depth —
       we want the dashboard unreachable from public internet even if
       UFW were misconfigured.

    2. test_dashboard_validates_bearer_token
       Render the app.py template; statically verify it has bearer-token
       validation logic (Authorization header parsing + constant-time
       compare against PHONE_HOME_TOKEN, returns 401 on mismatch).

    3. test_dashboard_db_schema
       Render app.py; statically grep for the expected CREATE TABLE
       statement so a refactor that drops/renames a column is caught
       at test time, not at first deploy.

    4. test_dashboard_systemd_unit_renders
       Golden-file compare of the .service unit template.

Plus belt-and-suspenders: orchestration wiring, host-gating discipline,
get_tenant_config usage, no-data-content rendering paths.

Run with: python3.12 -m pytest lib/test_dashboard.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyinfra" / "templates"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "monitoring"
MONITORING_TASKS_DIR = REPO_ROOT / "pyinfra" / "tasks" / "monitoring"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Default render kwargs for dashboard-app.py.j2 — matches what
# dashboard.apply() passes for bubble-internal.
_DEFAULT_APP_RENDER_KWARGS = {
    "tenant_name": "bubble-internal",
    "decrypted_runtime_path": "/run/claude-agent/env",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, **kwargs) -> str:
    return _jinja_env().get_template(template_name).render(**kwargs)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


# ─── 1. systemd unit binds via tailscale ip -4 ──────────────────────────────


def test_dashboard_binds_to_tailscale_ip_via_envfile():
    """SPEC-015 §"Critical": BIND_ADDR is derived from `tailscale ip -4`
    at unit start time via an ExecStartPre that writes
    /run/bubble-dashboard.env. The dashboard process reads BIND_ADDR
    from there. This is defense in depth — we never want 0.0.0.0 binding.
    """
    rendered = _render("bubble-dashboard.service.j2")

    # Must have an ExecStartPre that runs `tailscale ip -4`.
    pre_pattern = re.compile(
        r"ExecStartPre=.*tailscale\s+ip\s+-4.*?>\s*/run/bubble-dashboard\.env",
        re.MULTILINE,
    )
    assert pre_pattern.search(rendered), (
        "bubble-dashboard.service does NOT have an ExecStartPre that "
        "writes BIND_ADDR=$(tailscale ip -4) to /run/bubble-dashboard.env. "
        "Per SPEC-015, the bind addr must be derived at start time, not "
        "hardcoded. See SPEC-015 §'Critical' for rationale."
    )

    # Must reference EnvironmentFile=-/run/bubble-dashboard.env (the `-`
    # prefix makes it optional so unit-load doesn't fail before
    # ExecStartPre runs — documented gotcha in implementation-log.md).
    envfile_pattern = re.compile(
        r"EnvironmentFile=-?/run/bubble-dashboard\.env"
    )
    assert envfile_pattern.search(rendered), (
        "bubble-dashboard.service is missing EnvironmentFile for "
        "/run/bubble-dashboard.env. Without it, the BIND_ADDR derived "
        "by ExecStartPre never reaches the python process."
    )

    # The `-` prefix on EnvironmentFile is mandatory (file is created by
    # ExecStartPre; without `-` systemd fails at load time).
    assert "EnvironmentFile=-/run/bubble-dashboard.env" in rendered, (
        "EnvironmentFile entry for /run/bubble-dashboard.env must use the "
        "`-` prefix (optional) — the file is created by ExecStartPre, so "
        "without the prefix systemd fails at unit-load before "
        "ExecStartPre runs. Documented gotcha."
    )

    # MUST NOT hardcode 0.0.0.0 or a literal tailnet IP as BIND_ADDR.
    assert not re.search(r"BIND_ADDR=0\.0\.0\.0", rendered), (
        "bubble-dashboard.service hardcodes BIND_ADDR=0.0.0.0 — that "
        "exposes the dashboard on every interface including the public "
        "WAN. Per SPEC-015, derive from `tailscale ip -4` instead."
    )
    # Allow a literal Tailscale CGNAT IP ({{INTERNAL_IP}}/10) only inside comments
    # (anchored to # on the same line). Outside comments it must be the
    # variable derived at start time from `tailscale ip -4`.
    cgnat_re = re.compile(
        r"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}"
    )
    bad_lines = [
        line for line in rendered.splitlines()
        if cgnat_re.search(line) and not line.lstrip().startswith("#")
    ]
    assert not bad_lines, (
        f"bubble-dashboard.service hardcodes a tailnet (CGNAT) IP OUTSIDE a "
        f"comment: {bad_lines!r}. The unit must derive at start time so "
        f"re-registration doesn't strand it on a dead IP."
    )


# ─── 2. app.py validates Bearer token ───────────────────────────────────────


def test_dashboard_validates_bearer_token():
    """The app.py template must implement Bearer-token auth: parse the
    Authorization header, constant-time compare against the loaded
    PHONE_HOME_TOKEN, return 401 on mismatch. SPEC-015 + SPEC-008."""
    rendered = _render("dashboard-app.py.j2", **_DEFAULT_APP_RENDER_KWARGS)

    # Must reference Authorization header.
    assert re.search(r'self\.headers\.get\(\s*[\'"]Authorization[\'"]', rendered), (
        "app.py never reads the Authorization header — bearer auth missing."
    )

    # Must reference Bearer scheme.
    assert "Bearer" in rendered, (
        "app.py never references 'Bearer' — wrong auth scheme."
    )

    # Must use a constant-time compare (hmac.compare_digest) — `==` would
    # be a timing-attack surface.
    assert re.search(r"hmac\.compare_digest\s*\(", rendered), (
        "app.py compares the bearer token without hmac.compare_digest. "
        "A `==` compare leaks length + first-mismatch via timing. Use "
        "hmac.compare_digest for constant-time compare."
    )

    # Must return 401 (UNAUTHORIZED) on missing/invalid bearer.
    # We accept both `HTTPStatus.UNAUTHORIZED` and the raw int 401.
    assert re.search(r"HTTPStatus\.UNAUTHORIZED|\b401\b", rendered), (
        "app.py never returns 401 on auth failure — the auth check is "
        "not enforced."
    )

    # Must read PHONEHOME_TOKEN from the env file (not env var, since
    # systemd doesn't unpack arbitrary env file values into the
    # process environment).
    assert "PHONEHOME_TOKEN" in rendered, (
        "app.py never references PHONEHOME_TOKEN — token loading missing."
    )


# ─── 3. DB schema ───────────────────────────────────────────────────────────


def test_dashboard_db_schema():
    """The CREATE TABLE statement must match SPEC-015 §"Storage"."""
    rendered = _render("dashboard-app.py.j2", **_DEFAULT_APP_RENDER_KWARGS)

    # Required columns for the heartbeats table per SPEC-015.
    assert "CREATE TABLE" in rendered and "heartbeats" in rendered, (
        "app.py never creates a `heartbeats` table — storage missing."
    )
    # Each column must appear in the CREATE TABLE block.
    for col in (
        "id INTEGER PRIMARY KEY",
        "tenant_name TEXT NOT NULL",
        "ts_utc TEXT NOT NULL",
        "payload_json TEXT NOT NULL",
    ):
        assert col in rendered, (
            f"CREATE TABLE missing column declaration: {col!r}. "
            f"Per SPEC-015 §'Storage', the schema is fixed."
        )

    # The (tenant_name, ts_utc) index is required per spec.
    assert re.search(
        r"CREATE\s+INDEX[^;]*idx_tenant_ts[^;]*heartbeats\s*\(\s*tenant_name\s*,\s*ts_utc\s+DESC\s*\)",
        rendered,
    ), (
        "app.py is missing the (tenant_name, ts_utc DESC) index on "
        "heartbeats. Per SPEC-015 §'Storage', this index is required for "
        "the latest-per-tenant query to be fast as heartbeats grow."
    )


# ─── 4. Golden compare for systemd unit ─────────────────────────────────────


def test_dashboard_systemd_unit_renders():
    """Render bubble-dashboard.service.j2; compare to the committed golden."""
    rendered = _render("bubble-dashboard.service.j2")
    expected = _golden("bubble-dashboard.service")
    assert rendered == expected, (
        f"bubble-dashboard.service template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


# ─── pyinfra task module — orchestration wiring + discipline ────────────────


def test_top_level_deploy_imports_dashboard():
    """deploy.py must import dashboard and call its apply() after
    phone_home.apply(). Order: ... → phone_home → dashboard → hello."""
    deploy_path = REPO_ROOT / "deploy.py"
    source = deploy_path.read_text(encoding="utf-8")
    assert "dashboard" in source, (
        "Top-level deploy.py does not reference dashboard — the Task C "
        "central dashboard module is never invoked."
    )
    assert re.search(r"dashboard\.apply\s*\(", source), (
        "Top-level deploy.py never calls dashboard.apply()."
    )
    ph_pos = source.find("phone_home.apply")
    dash_pos = source.find("dashboard.apply")
    assert ph_pos != -1 and dash_pos != -1, (
        "Both phone_home.apply and dashboard.apply must be present in deploy.py"
    )
    assert ph_pos < dash_pos, (
        f"dashboard.apply (pos {dash_pos}) is invoked BEFORE "
        f"phone_home.apply (pos {ph_pos}). Per SPEC-015 ordering, "
        f"phone_home must come first (the daemon depends on the dashboard "
        f"existing — not at install time but conceptually)."
    )


def test_dashboard_pyinfra_module_uses_get_tenant_config():
    """Same architectural invariant: read tenant config via
    lib.host_helpers.get_tenant_config, never parse YAML directly.
    """
    module_path = MONITORING_TASKS_DIR / "dashboard.py"
    source = module_path.read_text(encoding="utf-8")
    assert re.search(
        r"from\s+lib\.host_helpers\s+import[^\n]*\bget_tenant_config\b",
        source,
    ), (
        "dashboard.py must import get_tenant_config from "
        "lib.host_helpers — direct YAML parsing in pyinfra tasks bypasses "
        "the SPEC-003 host-data exposure policy."
    )


def test_dashboard_pyinfra_module_gates_on_dashboard_host_tenant():
    """v1: the dashboard task must hardcode-gate on the dashboard-host
    tenant (bubble-internal). Other tenants no-op. When tenant #2 lands,
    this gate becomes a tenant.yaml schema field."""
    module_path = MONITORING_TASKS_DIR / "dashboard.py"
    source = module_path.read_text(encoding="utf-8")
    # Must reference "bubble-internal" as the dashboard host (in code, not
    # just docs).
    assert "bubble-internal" in source, (
        "dashboard.py does not reference bubble-internal — the v1 host "
        "gate is missing. Without this gate, every tenant would try to "
        "install the dashboard."
    )
    # Must compare cfg.tenant_name (or a host-data field) against it.
    assert re.search(
        r"(cfg\.tenant_name|tenant_name)\s*[!=]=\s*(_DASHBOARD_HOST_TENANT|[\"']bubble-internal[\"'])",
        source,
    ), (
        "dashboard.py must compare cfg.tenant_name against the dashboard-"
        "host constant before installing. Without the gate, every tenant "
        "tries to drop the systemd service."
    )


def test_dashboard_pyinfra_module_drops_systemd_unit():
    """Static check the module installs the systemd service unit."""
    module_path = MONITORING_TASKS_DIR / "dashboard.py"
    source = module_path.read_text(encoding="utf-8")
    assert "/etc/systemd/system/bubble-dashboard.service" in source
    assert re.search(r"files\.template\s*\(", source)


def test_dashboard_module_creates_db_dir_with_safe_perms():
    """SPEC-015 §"SPEC-008 hard rule compliance": the SQLite DB lives in
    /var/lib/bubble-dashboard/. Mode 0750 owner claude:claude — others
    have no access. We want the static guarantee, not just runtime hope.
    """
    module_path = MONITORING_TASKS_DIR / "dashboard.py"
    source = module_path.read_text(encoding="utf-8")
    assert "/var/lib/bubble-dashboard" in source, (
        "dashboard.py doesn't reference /var/lib/bubble-dashboard"
    )

    # Locate the files.directory call for the DB dir; assert mode=0750.
    def _slice_calls(src: str, fn: str) -> list[str]:
        out = []
        idx = 0
        marker = f"{fn}("
        while True:
            i = src.find(marker, idx)
            if i == -1:
                break
            depth = 0
            j = i + len(marker) - 1
            for k in range(j, len(src)):
                ch = src[k]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        out.append(src[i:k + 1])
                        idx = k + 1
                        break
            else:
                break
        return out

    matched = False
    for call in _slice_calls(source, "files.directory"):
        if "/var/lib/bubble-dashboard" in call or "_DB_DIR" in call:
            matched = True
            assert re.search(r'\bmode\s*=\s*["\']0?750["\']', call), (
                f"DB dir files.directory call must set mode='0750' "
                f"(others=no access; the SQLite DB stores heartbeat "
                f"metadata that an attacker could mine). Got: {call!r}"
            )
            assert re.search(r'\buser\s*=\s*["\']claude["\']', call), (
                f"DB dir files.directory call must set user='claude' so "
                f"the dashboard process (which runs as claude) can write. "
                f"Got: {call!r}"
            )
            break
    assert matched, (
        "dashboard.py doesn't create /var/lib/bubble-dashboard/ via "
        "files.directory(...). The DB dir won't exist at first start."
    )


# ─── 5. Render-path safety: no payload echoed in HTML ───────────────────────


def test_dashboard_html_escapes_user_input():
    """The HTML rendering paths read tenant_name + payload fields from
    SQLite (which originally came from a heartbeat POST). Even though
    we validate tenant_name at insert time, defense-in-depth: every
    field we render to HTML must go through html.escape().
    """
    rendered = _render("dashboard-app.py.j2", **_DEFAULT_APP_RENDER_KWARGS)
    # The render functions must import + use html.escape.
    assert re.search(r"^import\s+html\b|^from\s+html\b", rendered, re.MULTILINE), (
        "app.py doesn't import the html module — render paths can't escape."
    )
    # Must call html.escape on tenant_name in the render.
    assert "html.escape" in rendered, (
        "app.py never calls html.escape — XSS surface in the dashboard."
    )


def test_dashboard_does_not_log_authorization_header():
    """SPEC-008 hard rule extension: the dashboard's request log MUST NOT
    emit the Authorization header (would echo the token into journald).
    """
    rendered = _render("dashboard-app.py.j2", **_DEFAULT_APP_RENDER_KWARGS)
    # Locate any logger that might dump headers.
    # We look for the patterns: `self.headers.get("Authorization")` followed
    # by a log/print call WITHIN ~200 chars. None should exist.
    bad_pattern = re.compile(
        r'self\.headers\.get\([\'"]Authorization[\'"][^)]*\)[^\n]{0,400}'
        r'(self\.log_message|sys\.stdout\.write|print\s*\()',
        re.DOTALL,
    )
    matches = bad_pattern.findall(rendered)
    assert not matches, (
        "app.py logs the Authorization header value somewhere. Per "
        "SPEC-008, the header (which contains the bearer token) MUST "
        "NEVER reach a log sink."
    )


def test_dashboard_does_not_log_payload_body():
    """SPEC-015 §"SPEC-008 hard rule compliance": dashboard logs POST size
    + tenant_name + timestamp, NOT the payload body. The do_POST handler
    must not write `body` (the bytes it just read) to a log/print.
    """
    rendered = _render("dashboard-app.py.j2", **_DEFAULT_APP_RENDER_KWARGS)
    # Look for `body` (the raw payload bytes) being passed to a log call.
    # We accept `body.decode(...)` -> `payload = json.loads(...)` chain,
    # but `payload` should not flow into `log_message`/`print` either.
    bad_log_patterns = [
        r"self\.log_message\([^)]*\b(body|payload)\b",
        r"sys\.stdout\.write\([^)]*\bbody\b",
        r"print\s*\([^)]*\bbody\b",
    ]
    for pat in bad_log_patterns:
        m = re.search(pat, rendered)
        assert not m, (
            f"app.py logs the heartbeat body/payload (pattern {pat!r} "
            f"matched: {m.group(0)!r}). Per SPEC-015, log only the FACT "
            f"of receipt — tenant + ts + bytes — never the payload."
        )


# ─── 6. Sudo-escalation static guard (regression: 2026-05-31 deploy blocker) ──
#
# The deploy connects AS claude (tenant ssh_user: claude). dashboard's apply()
# writes BOTH root-owned files (/etc/systemd/system/bubble-dashboard.service)
# AND claude-owned files (/home/claude/dashboard/{,app.py}), creates a
# root-territory dir (/var/lib/bubble-dashboard/, chowned to claude), and runs
# root-only systemctl commands (daemon-reload, enable/start/restart the
# service). Each must escalate correctly or pyinfra dies with `[Errno 13]
# Permission denied` → `No hosts remaining!`. These AST tests pin the
# escalation kwargs so a refactor can't silently drop them. Mirror of
# TestCloudWikiSyncSudoEscalation (lib/test_cloud_wiki_sync.py) — the freshest
# instance of the same gap fixed in commit 8f6fbec (agent + watchdog) and
# cloud_wiki_sync.
#
# RULE: root target (/etc, /usr) → `_sudo=True` ALONE (NO _sudo_user — writing
#       as claude to /etc is still Permission denied); claude target
#       (/home/claude) → `_sudo=True, _sudo_user="claude"`; root-only command
#       (systemctl/daemon-reload) → `_sudo=True`. The /var/lib DB dir is
#       root-territory (claude can't mkdir there) → `_sudo=True` ALONE; pyinfra
#       chowns it to claude via the user=/group= kwargs.


def _dash_assign_env(tree):
    import ast

    env: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(n.value)
    return env


def _dash_str_fragments(node, env, _depth=0):
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
                out |= _dash_str_fragments(v.value, env, _depth + 1)
    elif isinstance(node, ast.Name):
        for val in env.get(node.id, []):
            out |= _dash_str_fragments(val, env, _depth + 1)
    elif isinstance(node, ast.BinOp):
        out |= _dash_str_fragments(node.left, env, _depth + 1)
        out |= _dash_str_fragments(node.right, env, _depth + 1)
    return out


def _dash_has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def _dash_kw_is_claude(node, name):
    import ast

    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value == "claude"
    return False


def _dash_classify(fragments):
    # Root territory: /etc, /usr (systemd unit), AND /var/lib (root-owned
    # parent — claude can't mkdir there, so the op runs as root and chowns the
    # result to claude). Only /home/claude paths are claude-escalated.
    if any(
        f.startswith("/etc/") or f.startswith("/usr/") or f.startswith("/var/")
        for f in fragments
    ):
        return "root"
    if any("/home/claude" in f for f in fragments):
        return "claude"
    return "unknown"


def _dash_ops(tree):
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


def _dash_command_strings(node):
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


class TestDashboardSudoEscalation:
    """dashboard.py escalates every root write / root command and uses
    _sudo_user=claude for the /home/claude files. AST guard for the
    2026-05-31 deploy blocker — same gap fixed in the agent + watchdog tasks
    (commit 8f6fbec) and cloud_wiki_sync.py."""

    def _tree(self):
        import ast

        path = MONITORING_TASKS_DIR / "dashboard.py"
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_etc_writes_have_sudo_no_sudo_user(self):
        tree = self._tree()
        env = _dash_assign_env(tree)
        found_root = 0
        for qual, node in _dash_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _dash_str_fragments(kw.value, env)
            if _dash_classify(frags) == "root":
                found_root += 1
                assert _dash_has_kw(node, "_sudo"), (
                    f"dashboard: {qual} writing root path {sorted(frags)} "
                    f"is MISSING _sudo=True (deploy connects AS claude)."
                )
                assert not _dash_has_kw(node, "_sudo_user"), (
                    f"dashboard: {qual} writing root path {sorted(frags)} "
                    f"must NOT set _sudo_user (escalate to root, not claude — "
                    f"writing as claude to /etc or mkdir under /var/lib is "
                    f"still Permission denied)."
                )
        # service unit (/etc) + DB dir (/var/lib) = two root-territory writes.
        assert found_root >= 2, (
            f"Expected >=2 root-owned writes in dashboard.py (the "
            f"/etc/systemd/system/bubble-dashboard.service unit + the "
            f"/var/lib/bubble-dashboard DB dir), classifier found {found_root}."
        )

    def test_home_claude_writes_have_sudo_user_claude(self):
        tree = self._tree()
        env = _dash_assign_env(tree)
        found_claude = 0
        for qual, node in _dash_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _dash_str_fragments(kw.value, env)
            if _dash_classify(frags) == "claude":
                found_claude += 1
                assert _dash_has_kw(node, "_sudo"), (
                    f"dashboard: {qual} writing claude path "
                    f"{sorted(frags)} is MISSING _sudo=True."
                )
                assert _dash_kw_is_claude(node, "_sudo_user"), (
                    f"dashboard: {qual} writing claude path "
                    f"{sorted(frags)} must set _sudo_user=\"claude\"."
                )
        # /home/claude/dashboard/ dir + app.py template = two claude writes.
        assert found_claude >= 2, (
            f"Expected >=2 claude-owned /home/claude writes (dashboard dir + "
            f"app.py), classifier found {found_claude}."
        )

    def test_systemctl_shell_commands_have_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _dash_ops(tree):
            if qual != "server.shell":
                continue
            joined = "\n".join(_dash_command_strings(node))
            if "systemctl " in joined or "journalctl " in joined:
                seen += 1
                assert _dash_has_kw(node, "_sudo"), (
                    f"dashboard: server.shell running {joined!r} is "
                    f"MISSING _sudo=True — systemctl is root-only."
                )
                # A root systemctl command must NOT be escalated to claude.
                assert not _dash_kw_is_claude(node, "_sudo_user"), (
                    f"dashboard: server.shell running {joined!r} must "
                    f"NOT set _sudo_user=claude — systemctl is root-only."
                )
        # daemon-reload + restart service = two root systemctl shells expected.
        assert seen >= 2, (
            f"Expected >=2 root systemctl server.shell ops (daemon-reload + "
            f"restart service), classifier found {seen}."
        )

    def test_systemd_service_op_has_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _dash_ops(tree):
            if qual != "systemd.service":
                continue
            seen += 1
            assert _dash_has_kw(node, "_sudo"), (
                "dashboard: systemd.service (enable/start the service) "
                "is MISSING _sudo=True — root-only operation."
            )
            assert not _dash_kw_is_claude(node, "_sudo_user"), (
                "dashboard: systemd.service must NOT set _sudo_user=claude — "
                "systemctl enable/start is root-only."
            )
        assert seen >= 1, (
            "Expected a systemd.service op (enable+start the service) in "
            "dashboard.py."
        )

    def test_db_dir_escalates_to_root_not_claude(self):
        """The /var/lib/bubble-dashboard DB dir is created in root-owned
        /var/lib/ (claude can't mkdir there). It must escalate to ROOT
        (`_sudo=True` ALONE) — NOT _sudo_user=claude, which would run
        `sudo -u claude mkdir /var/lib/...` and hit Permission denied. pyinfra
        then chowns the dir to claude:claude via the user=/group= kwargs."""
        tree = self._tree()
        env = _dash_assign_env(tree)
        db_nodes = []
        for qual, node in _dash_ops(tree):
            if qual != "files.directory":
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _dash_str_fragments(kw.value, env)
            if any("/var/lib/bubble-dashboard" in f for f in frags):
                db_nodes.append(node)
        assert len(db_nodes) == 1, (
            f"Expected exactly one files.directory for the /var/lib/"
            f"bubble-dashboard DB dir, found {len(db_nodes)}."
        )
        node = db_nodes[0]
        assert _dash_has_kw(node, "_sudo"), (
            "dashboard: the /var/lib/bubble-dashboard files.directory is "
            "MISSING _sudo=True — /var/lib is root-owned."
        )
        assert not _dash_has_kw(node, "_sudo_user"), (
            "dashboard: the /var/lib/bubble-dashboard files.directory must "
            "NOT set _sudo_user=claude — claude cannot mkdir under root-owned "
            "/var/lib. Run as root; pyinfra chowns the result to claude."
        )
