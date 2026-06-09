"""Static + golden-file tests for the daily cloud security audit cron
(Task B — SPEC-014).

Six surfaces covered (per SPEC-014 §"Test plan"):

    1. test_audit_script_no_plaintext_secrets
       Render the bash template with mock cfg, grep for known credential
       prefixes — must find ZERO matches. The script reads the bot token
       at runtime from /run/claude-agent/env; never bakes a secret into
       the rendered template.

    2. test_audit_script_uses_grep_l_for_transcript_scan
       Assert the script uses `grep -l` (filenames only) NOT bare `grep`
       for the Part 6 transcript scan. Bare grep would echo the matched
       line, which contains the credential VALUE we're hunting for —
       defeating the scan and creating a fresh leak.

    3. test_audit_script_unsets_token_after_use
       Same SPEC-008 hard rule extension as the watchdog: every curl
       using ${TOKEN} must be followed by `unset TOKEN`.

    4. test_audit_log_file_mode_0640
       Assert the script creates audit logs with mode 0640
       (`install -m 640` or `chmod 0640`). Logs are root:adm so the
       claude user can't read its own audit history.

    5. test_audit_pyinfra_module_drops_sudoers
       Static check the module installs the sudoers drop-in —
       /etc/sudoers.d/claude-security-audit must be present in the task
       module source, otherwise the audit can't `sudo fail2ban-client
       status sshd` etc.

    6. test_audit_systemd_units_render
       Golden compare for the .timer and .service unit templates.

Plus a few belt-and-suspenders extras (orchestration wiring, get_tenant_config
discipline, sudoers well-formed) that follow the test_telegram_watchdog.py
pattern.

Run with: python3.12 -m pytest lib/test_security_audit.py -v
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


# Literal credential prefixes that MUST NOT leak into the rendered audit
# script. Same set as test_telegram_watchdog.py + test_agent_layer.py.
LEAKED_PREFIXES = (
    "8350575119:",   # Telegram bot id (rotated)
    "sk-or-v1-",     # OpenRouter key prefix
    "sk-ant-oat01-", # Anthropic OAuth token prefix
    "tskey-auth-",   # Tailscale auth key prefix
)


# Default render kwargs — matches what security_audit.apply() passes for
# bubble-internal. The expected_box_pubkey is intentionally a plausible-but-
# fake value here (real one is in bubble-vps-data, not committed to platform
# repo). Keep these in sync if the module's defaults change.
_DEFAULT_RENDER_KWARGS = {
    "service_name": "claude-agent-morty.service",
    "decrypted_runtime_path": "/run/claude-agent/env",
    "joris_telegram_user_id": "6532205130",
    "audit_log_dir": "/var/log/bubble-security",
    "expected_box_pubkey": "age1examplepubkeyfortestonly000000000000000000000000000000000",
}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    """Mirror pyinfra's render env (default Environment + keep_trailing_newline)
    so byte-for-byte golden compares match what pyinfra ships to the box.
    """
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, **kwargs) -> str:
    return _jinja_env().get_template(template_name).render(**kwargs)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


# ─── 1. Audit script — no plaintext credentials ─────────────────────────────


def test_audit_script_no_plaintext_secrets():
    """Render the bash template with mock cfg, grep for known leaked-credential
    prefixes. The audit reads the token at runtime from a tmpfs env file —
    it must NEVER bake a credential into the template itself.

    Caveat: the audit's Part 6 transcript scan + Part 2 filesystem scan
    legitimately reference the credential PREFIXES as grep search patterns
    (e.g. `grep -rlI -e "sk-or-v1-" ...`). Those prefixes are the search
    needle, not credential values — they're 8-12 chars of structure with
    NO actual secret bytes after them. We allow occurrences inside `-e
    "<prefix>"` grep arguments and forbid them anywhere else.

    The test strips out lines containing `-e "<prefix>"` patterns before
    scanning for leaked literals. If a prefix appears anywhere ELSE
    (e.g. as `TOKEN=sk-or-v1-actualvaluehere`), the test fails loudly.
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # Strip lines that ONLY use the prefix as a grep search pattern.
    # Match: optional whitespace + `-e ` + quote + prefix + quote + optional ` \`.
    grep_pattern_re = re.compile(
        r'^\s*-e\s+["\'](?:sk-or-v1-|sk-ant-oat01-|tskey-auth-|\^?TELEGRAM_BOT_TOKEN=)["\']\s*\\?\s*$'
    )
    safe_lines = [
        line for line in rendered.splitlines()
        if not grep_pattern_re.match(line)
    ]
    cleaned = "\n".join(safe_lines)

    for prefix in LEAKED_PREFIXES:
        assert prefix not in cleaned, (
            f"Leaked credential prefix {prefix!r} found in rendered "
            f"security-audit.sh OUTSIDE of a `-e \"<prefix>\"` grep "
            f"pattern context — the script must read the token at "
            f"runtime from {_DEFAULT_RENDER_KWARGS['decrypted_runtime_path']}, "
            f"never embed an actual credential value as a template literal. "
            f"If you're adding a NEW grep search pattern, put it on its own "
            f"line in the form: `        -e \"<prefix>\" \\`."
        )


# ─── 2. Audit script — Part 6 uses grep -l (filenames only) ─────────────────


def test_audit_script_uses_grep_l_for_transcript_scan():
    """SPEC-014 §"Implementation > Module > Part 6" + SPEC-008 hard rule:
    the transcript-leak scan MUST use `grep -l` (filenames only) NOT bare
    `grep`. Bare grep echoes the matched line, which contains the
    credential VALUE — defeating the scan and creating a fresh leak.

    We accept any combination of grep flags as long as `-l` is present
    (e.g. `grep -lI`, `grep -rlI`, `grep -lir` all OK; bare `grep -r` is
    NOT OK).
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # Locate the transcript-leak scan section. Must be present.
    assert "transcripts" in rendered.lower(), (
        "Audit script doesn't reference transcripts at all — Part 6 missing?"
    )

    # Find every grep invocation in the script. We accept multi-line grep
    # calls (with `\` line continuations) — the regex matches `grep` plus
    # its first flag bundle, then we look ahead in the script for the path
    # argument to determine if it's the transcript scan or filesystem scan.
    grep_call_re = re.compile(r"\bgrep\s+(-[A-Za-z]+)\b")
    grep_call_matches = list(grep_call_re.finditer(rendered))
    assert grep_call_matches, "Audit script has no grep calls — Part 2 + Part 6 missing?"

    # For each grep call, extract the surrounding "block" — from the grep
    # keyword to the next blank line / closing brace / pipe. This captures
    # multi-line backslash-continued invocations.
    transcript_grep_blocks = []
    fs_scan_grep_blocks = []
    for m in grep_call_matches:
        # Take a window of up to 600 chars after the grep keyword (covers a
        # ~10-line continued invocation comfortably).
        block = rendered[m.start(): m.start() + 600]
        # Truncate at the first standalone closing structure (`)` followed
        # by newline, `;`, or end of pipeline).
        end = re.search(r"\)\s*\n", block)
        if end:
            block = block[: end.end()]
        if "$transcripts_dir" in block or "/.claude/projects" in block:
            transcript_grep_blocks.append((m.group(1), block))
        if "/home /etc /root" in block:
            fs_scan_grep_blocks.append((m.group(1), block))

    assert transcript_grep_blocks, (
        "Audit script doesn't grep the transcripts dir — Part 6 not "
        "implementing the leak scan."
    )
    for flags, block in transcript_grep_blocks:
        assert "l" in flags, (
            f"Transcript-scan grep call missing `-l` flag (filenames only); "
            f"bare grep without -l would echo the credential VALUE on match. "
            f"Got flags={flags!r} in block:\n{block!r}"
        )

    # Belt-and-suspenders: also assert the Part 2 secret-leak scan (which
    # searches /home /etc /root for plaintext credentials) uses -l. Same
    # rationale — we don't want the audit itself to leak the value.
    assert fs_scan_grep_blocks, (
        "Audit script doesn't grep /home /etc /root — Part 2 plaintext-leak "
        "scan missing."
    )
    for flags, block in fs_scan_grep_blocks:
        assert "l" in flags, (
            f"Filesystem-scan grep call missing -l flag — would echo "
            f"matched credential lines. flags={flags!r} block:\n{block!r}"
        )


# ─── 3. Audit script — every TOKEN use is followed by unset ─────────────────


def test_audit_script_unsets_token_after_use():
    """SPEC-008 hard rule extension: every `curl ... bot${TOKEN}` use must be
    followed by `unset TOKEN` before the script exits any path.
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)

    token_uses = re.findall(r"bot\$\{TOKEN\}/", rendered)
    unset_count = len(re.findall(r"\bunset\s+TOKEN\b", rendered))

    assert token_uses, (
        "Sanity check: the rendered audit script doesn't reference "
        "${TOKEN} at all in a curl URL. That means it's not actually "
        "talking to Telegram — broken script."
    )
    assert unset_count >= len(token_uses), (
        f"Audit script uses ${{TOKEN}} in {len(token_uses)} curl URL(s) "
        f"but only contains {unset_count} `unset TOKEN` statement(s). "
        f"SPEC-008 requires the token to be unset after every use."
    )

    # Also: the LAST reference to ${TOKEN} must be followed by `unset TOKEN`.
    last_use = max(m.start() for m in re.finditer(r"\$\{TOKEN\}", rendered))
    last_unset = max(
        (m.start() for m in re.finditer(r"\bunset\s+TOKEN\b", rendered)),
        default=-1,
    )
    assert last_unset > last_use, (
        "The last `${TOKEN}` reference in the audit script is NOT followed "
        "by an `unset TOKEN`. The token would linger in the shell env "
        "until the process exits — defense-in-depth violation."
    )


# ─── 4. Audit log file mode 0640 ────────────────────────────────────────────


def test_audit_log_file_mode_0640():
    """Per SPEC-014 §"SPEC-008 hard rule compliance":
    `/var/log/bubble-security/audit-<date>.log` must be mode 0640 root:adm.
    Verify the rendered script creates the log file with `install -m 640`
    (or equivalent `chmod 0640`).
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # We accept either `install -m 0640` / `install -m 640` or a `chmod 0640`
    # / `chmod 640` against the log file. install -m is preferred (atomic
    # mode at creation), but chmod also satisfies the spec's mode requirement.
    install_match = re.search(
        r"\binstall\s+(?:-[a-zA-Z]+\s+)*-m\s+0?640\b",
        rendered,
    )
    chmod_match = re.search(r"\bchmod\s+0?640\b", rendered)
    assert install_match or chmod_match, (
        "Audit script doesn't create the log file with mode 0640. "
        "Per SPEC-014, /var/log/bubble-security/audit-<date>.log must be "
        "0640 owner root:adm so a compromised claude agent can't read its "
        "own audit history. Use `install -m 640 -o root -g adm /dev/null "
        "$LOG_FILE` (preferred — atomic) or `chmod 0640 $LOG_FILE`."
    )

    # Belt-and-suspenders: verify the install-m form also pins owner=root
    # group=adm, so we don't accidentally regress to claude-owned logs.
    # Skip COMMENT lines (starting with `#` after optional indent) when
    # looking for the actual command — comments may legitimately mention
    # `install -m 640` without the full -o/-g flags as documentation.
    if install_match:
        install_lines = [
            line for line in rendered.splitlines()
            if re.search(r"\binstall\s+(?:-[a-zA-Z]+\s+)*-m\s+0?640\b", line)
            and not line.lstrip().startswith("#")
        ]
        assert install_lines, (
            "Found `install -m 640` only inside comments — no actual "
            "executable install command. Per SPEC-014, the audit script "
            "must atomically create the log file with `install -m 640 -o "
            "root -g adm /dev/null $LOG_FILE`."
        )
        for install_line in install_lines:
            assert "-o root" in install_line, (
                f"install -m 640 line doesn't pin -o root; the log could be "
                f"owned by claude, defeating the SPEC-014 access boundary. "
                f"Line: {install_line!r}"
            )
            assert "-g adm" in install_line, (
                f"install -m 640 line doesn't pin -g adm; per SPEC-014 the "
                f"log must be group-readable by adm. Line: {install_line!r}"
            )


# ─── 5. pyinfra task module — drops the sudoers rule ────────────────────────


def test_audit_pyinfra_module_drops_sudoers():
    """Read the pyinfra task module source, assert it includes a
    `files.template` (or `files.put`) targeting
    /etc/sudoers.d/claude-security-audit. Without this drop, the audit
    has no NOPASSWD privilege to run fail2ban-client / sshd -T / etc.
    """
    module_path = ACCESS_TASKS_DIR / "security_audit.py"
    assert module_path.is_file(), (
        f"pyinfra task module missing: {module_path}. "
        f"This is the entry point for SPEC-014."
    )
    source = module_path.read_text(encoding="utf-8")

    # Must reference the sudoers path
    assert "/etc/sudoers.d/claude-security-audit" in source, (
        "security_audit.py does not reference "
        "/etc/sudoers.d/claude-security-audit. Without this drop, the "
        "audit can't run its elevated-read commands."
    )

    # Must invoke files.template or files.put.
    assert re.search(r"files\.(template|put)\s*\(", source), (
        "security_audit.py doesn't use files.template or files.put — "
        "the sudoers rule never reaches the box."
    )

    # Sanity: the sudoers template itself must exist.
    sudoers_template = TEMPLATES_DIR / "sudoers-security-audit.j2"
    assert sudoers_template.is_file(), (
        f"sudoers template missing at {sudoers_template}. "
        f"Even if security_audit.py references the path, without the "
        f"template file pyinfra will fail at render-time."
    )


def test_audit_sudoers_template_well_formed():
    """Sanity-check the rendered sudoers content: single NOPASSWD rule line
    targeting the claude user + the read commands the audit needs. No shell
    metacharacters that would let claude escape the sudoers scope.
    """
    rendered = _render("sudoers-security-audit.j2")

    # Must contain the canonical NOPASSWD line.
    assert "claude ALL=(ALL) NOPASSWD:" in rendered, (
        "Sudoers template missing the canonical NOPASSWD rule prefix."
    )
    # Must reference the SPEC-014 read commands.
    assert "/usr/bin/fail2ban-client status sshd" in rendered
    assert "/usr/sbin/sshd -T" in rendered
    assert "/usr/bin/last -F -50" in rendered
    assert "/bin/cat /etc/sudoers.d/*" in rendered
    assert "/bin/cat /etc/passwd" in rendered
    assert "/usr/bin/journalctl" in rendered

    # No shell metacharacters that would let claude escape sudoers scope.
    rule_lines = [
        line for line in rendered.splitlines()
        if line and not line.lstrip().startswith("#")
    ]
    for line in rule_lines:
        for bad in (";", "&&", "||", "`", "$("):
            assert bad not in line, (
                f"Sudoers rule contains shell metacharacter {bad!r}: {line!r}"
            )


# ─── 6. Golden-file compare for systemd timer + service ─────────────────────


def test_audit_systemd_timer_renders():
    """Render security-audit.timer.j2; compare to the committed golden."""
    rendered = _render("security-audit.timer.j2")
    expected = _golden("security-audit.timer")
    assert rendered == expected, (
        f"security-audit.timer template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_audit_systemd_service_renders():
    """Render security-audit.service.j2; compare to the committed golden."""
    rendered = _render("security-audit.service.j2")
    expected = _golden("security-audit.service")
    assert rendered == expected, (
        f"security-audit.service template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


# ─── Audit script structural sanity ─────────────────────────────────────────


def test_audit_script_has_all_eight_parts():
    """SPEC-014 §"What it checks" defines 8 parts. Verify each is present
    (catches a refactor that drops one).
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)
    for n in range(1, 9):
        # Each part is implemented as a `part_<n>_*()` function
        assert re.search(rf"\bpart_{n}_[a-z_]+\b", rendered), (
            f"Audit script missing Part {n} function (part_{n}_*)"
        )


def test_audit_script_uses_set_uo_pipefail_not_e():
    """Per SPEC-014 (and the watchdog's same rationale): each part is
    independent; one failed check shouldn't abort the whole audit.
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "set -uo pipefail" in rendered, (
        "Audit script must `set -uo pipefail` (NOT -e); each part is "
        "independent and one failure shouldn't abort subsequent parts."
    )


# ─── 7. Orchestration wiring + get_tenant_config discipline ─────────────────


def test_top_level_deploy_imports_security_audit():
    """deploy.py must import the audit task module and call its apply()
    after telegram_watchdog.apply(). Order: hardening → secrets → agent →
    tailscale → telegram_watchdog → security_audit → hello.
    """
    deploy_path = REPO_ROOT / "deploy.py"
    source = deploy_path.read_text(encoding="utf-8")
    assert "security_audit" in source, (
        "Top-level deploy.py does not reference security_audit — the "
        "Task B module is never invoked at deploy time."
    )
    assert re.search(r"security_audit\.apply\s*\(", source), (
        "Top-level deploy.py never calls security_audit.apply()."
    )
    # Must come AFTER telegram_watchdog.apply()
    watchdog_pos = source.find("telegram_watchdog.apply")
    audit_pos = source.find("security_audit.apply")
    assert watchdog_pos != -1 and audit_pos != -1, (
        "Both telegram_watchdog.apply and security_audit.apply must be present"
    )
    assert watchdog_pos < audit_pos, (
        f"security_audit.apply (pos {audit_pos}) is invoked BEFORE "
        f"telegram_watchdog.apply (pos {watchdog_pos}). Per SPEC-014 "
        f"ordering, telegram_watchdog must come first."
    )


def test_pyinfra_module_uses_get_tenant_config():
    """Same architectural invariant as the secrets + agent + watchdog layers:
    read tenant config via lib.host_helpers.get_tenant_config, never parse
    YAML directly.
    """
    module_path = ACCESS_TASKS_DIR / "security_audit.py"
    source = module_path.read_text(encoding="utf-8")
    assert re.search(
        r"from\s+lib\.host_helpers\s+import[^\n]*\bget_tenant_config\b",
        source,
    ), (
        "security_audit.py must import get_tenant_config from "
        "lib.host_helpers — direct YAML parsing in pyinfra tasks bypasses "
        "the SPEC-003 host-data exposure policy."
    )


def test_pyinfra_module_creates_log_dir_root_adm():
    """SPEC-014 §"Implementation": /var/log/bubble-security/ must be created
    by pyinfra with mode 0750 owner root:adm so a compromised claude can't
    read or list its own audit history.
    """
    module_path = ACCESS_TASKS_DIR / "security_audit.py"
    source = module_path.read_text(encoding="utf-8")
    assert "/var/log/bubble-security" in source, (
        "security_audit.py doesn't reference /var/log/bubble-security"
    )
    # Look for a files.directory call with the audit log dir + group="adm".
    # We can't trivially match nested-paren expressions with regex, so we
    # parse loosely: locate every `files.directory(` call by scanning forward
    # to the matching close-paren (depth-1 brace count), then check the body
    # for the (path-substring, group="adm") pair together.
    def _slice_calls(src: str, fn: str) -> list[str]:
        out = []
        idx = 0
        marker = f"{fn}("
        while True:
            i = src.find(marker, idx)
            if i == -1:
                break
            depth = 0
            j = i + len(marker) - 1  # index of the opening "("
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
                break  # unmatched paren — stop scanning
        return out

    dir_calls = _slice_calls(source, "files.directory")
    matched = False
    for call in dir_calls:
        if (
            ("/var/log/bubble-security" in call or "_AUDIT_LOG_DIR" in call)
            and re.search(r'\bgroup\s*=\s*["\']adm["\']', call)
        ):
            matched = True
            # Belt-and-suspenders: must also be owner=root and mode=0750
            assert re.search(r'\buser\s*=\s*["\']root["\']', call), (
                f"audit log dir files.directory call references the right "
                f"path + group but doesn't pin user='root': {call!r}"
            )
            assert re.search(r'\bmode\s*=\s*["\']0?750["\']', call), (
                f"audit log dir files.directory call doesn't set mode='0750'. "
                f"Per SPEC-014, /var/log/bubble-security/ must be 0750 so "
                f"non-adm users can't list audit files. Got: {call!r}"
            )
            break

    assert matched, (
        "security_audit.py must create /var/log/bubble-security/ with "
        "group='adm' (owner root); per SPEC-014 the audit log dir must "
        "NOT be readable by the claude user."
    )


# ─── Part 2 leak-scan: documentation locations must be excluded ───────────


def test_part2_excludes_shared_wiki():
    """SPEC-014 Part 2 (Secrets layer leak scan): the cross-agent wiki at
    `~/.claude/agent-memory/shared-wiki/` LEGITIMATELY contains credential
    prefix names in documentation, post-mortems, and concept pages — it
    NEVER contains real credential values (verified at lockdown time).

    Two false-positive incidents drove this exclusion:
    - 2026-05-09→11: `hetzner-migration/STATUS.md` line "settings.json
      contains NO `sk-or-v1`, `sk-ant-oat01-`, or token prefixes" tripped
      grep for 3 days running.
    - 2026-05-12: nightly wiki-compile regenerated `rnd/morty-agent.md`
      summarizing the prior incident, faithfully quoting the prefix name
      → grep matched again, fresh false-positive alert.

    The fix: same pattern as the existing `bubble-vps-platform/` exclude —
    documentation about credentials is not a credential leak. Tightening
    the regex to require a credential-shaped suffix was considered but
    rejected (would weaken defense-in-depth against bare-prefix paste-jobs).
    """
    template = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Must exclude shared-wiki/ from the leak scan
    assert "--exclude-dir=shared-wiki" in template, (
        "security-audit.sh.j2 Part 2 must `--exclude-dir=shared-wiki` to "
        "prevent false-positive plaintext-leak alerts from documentation "
        "pages that reference credential prefixes by name. See SPEC-014 + "
        "2026-05-11/12 incident notes."
    )


def test_part2_still_excludes_known_safe_locations():
    """Regression guard: when adding the shared-wiki exclusion, don't
    accidentally drop the other intentional excludes."""
    template = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)
    required_excludes = [
        "--exclude-dir=/etc/bubble",          # encrypted blob lives here
        "--exclude-dir=/run",                 # tmpfs runtime env
        "--exclude-dir=bubble-vps-platform",  # spec docs reference prefixes
        "--exclude-dir=shared-wiki",          # NEW 2026-05-12
        "--exclude=security-audit.sh",        # the script itself
        "--exclude=.credentials.json",        # claude CLI's own login store
    ]
    for needle in required_excludes:
        assert needle in template, (
            f"security-audit.sh.j2 Part 2 must keep `{needle}` in the leak-scan "
            f"exclusions — dropping it would resurface a known false-positive "
            f"or expose a known-safe location to the scan."
        )


def test_telegram_post_respects_agentic_suppress_flag():
    """SPEC-014 + 2026-05-12 incident: when invoked by the morty-agentic-audit
    cron as a subprocess, the shell audit must SKIP its direct Telegram
    emission so the agentic cron can send the single consolidated brief.

    The legacy raw-telegram path was confirmed as duplicate noise on
    2026-05-12 (Joris received the raw 73/80 brief at 09:00 UTC even though
    the agentic cron at 10:00 UTC also sent a brief). The fix: a sentinel
    env var `AGENTIC_AUDIT_SUPPRESS_TELEGRAM=1` makes the script skip the
    curl-to-telegram block while still writing to /var/log/bubble-security/.
    """
    rendered = _render("security-audit.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "AGENTIC_AUDIT_SUPPRESS_TELEGRAM" in rendered, (
        "security-audit.sh.j2 must support AGENTIC_AUDIT_SUPPRESS_TELEGRAM=1 "
        "to allow morty-agentic-audit to invoke it without firing a duplicate "
        "Telegram message. See 2026-05-12 incident in SPEC-014."
    )
    # Suppression must precede the actual curl call — verify the check sits
    # before the api.telegram.org line in the script.
    suppress_idx = rendered.find("AGENTIC_AUDIT_SUPPRESS_TELEGRAM")
    curl_idx = rendered.find("api.telegram.org/bot")
    assert suppress_idx > 0 and curl_idx > 0 and suppress_idx < curl_idx, (
        "AGENTIC_AUDIT_SUPPRESS_TELEGRAM check must appear BEFORE the "
        "telegram curl invocation — otherwise the curl fires regardless."
    )


# ─── Sudo-escalation static guard (regression: 2026-05-31 deploy blocker) ─────
#
# The deploy connects AS claude (tenant ssh_user: claude). security_audit's
# apply() writes BOTH root-owned files (/etc/sudoers.d/claude-security-audit,
# /etc/systemd/system/security-audit.{timer,service}), creates a root-territory
# log dir (/var/log/bubble-security/, root:adm), AND claude-owned files
# (/home/claude/scripts/{,security-audit.sh}), then runs root-only systemctl
# commands (daemon-reload, enable/start, restart the timer). Each must escalate
# correctly or pyinfra dies with `[Errno 13] Permission denied` →
# `No hosts remaining!`. These AST tests pin the escalation kwargs so a refactor
# can't silently drop them. Mirror of TestDashboardSudoEscalation
# (lib/test_dashboard.py) + TestCloudWikiSyncSudoEscalation — the same gap fixed
# in commit 8f6fbec (agent + watchdog), cloud_wiki_sync, and dashboard.
#
# RULE: root target (/etc, /usr, /var/log, /var/lib) → `_sudo=True` ALONE (NO
#       _sudo_user — writing as claude to /etc, or mkdir under root-owned
#       /var/log, is still Permission denied; pyinfra chowns the dir to its
#       user=/group= afterward); claude target (/home/claude) → `_sudo=True,
#       _sudo_user="claude"`; root-only command (systemctl/daemon-reload) →
#       `_sudo=True`.


def _sa_assign_env(tree):
    import ast

    env: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(n.value)
    return env


def _sa_str_fragments(node, env, _depth=0):
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
                out |= _sa_str_fragments(v.value, env, _depth + 1)
    elif isinstance(node, ast.Name):
        for val in env.get(node.id, []):
            out |= _sa_str_fragments(val, env, _depth + 1)
    elif isinstance(node, ast.BinOp):
        out |= _sa_str_fragments(node.left, env, _depth + 1)
        out |= _sa_str_fragments(node.right, env, _depth + 1)
    return out


def _sa_has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def _sa_kw_is_claude(node, name):
    import ast

    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value == "claude"
    return False


def _sa_classify(fragments):
    # Root territory: /etc, /usr (sudoers + systemd units), /var/log (the
    # audit log dir — root-owned parent, claude can't mkdir there), and
    # /var/lib for completeness. Only /home/claude paths are claude-escalated.
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


def _sa_ops(tree):
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


def _sa_command_strings(node):
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


class TestSecurityAuditSudoEscalation:
    """security_audit.py escalates every root write / root command and uses
    _sudo_user=claude for the /home/claude files. AST guard for the
    2026-05-31 deploy blocker — same gap fixed in the agent + watchdog tasks
    (commit 8f6fbec), cloud_wiki_sync.py, and dashboard.py."""

    def _tree(self):
        import ast

        path = ACCESS_TASKS_DIR / "security_audit.py"
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_root_writes_have_sudo_no_sudo_user(self):
        tree = self._tree()
        env = _sa_assign_env(tree)
        found_root = 0
        for qual, node in _sa_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _sa_str_fragments(kw.value, env)
            if _sa_classify(frags) == "root":
                found_root += 1
                assert _sa_has_kw(node, "_sudo"), (
                    f"security_audit: {qual} writing root path {sorted(frags)} "
                    f"is MISSING _sudo=True (deploy connects AS claude)."
                )
                assert not _sa_has_kw(node, "_sudo_user"), (
                    f"security_audit: {qual} writing root path {sorted(frags)} "
                    f"must NOT set _sudo_user (escalate to root, not claude — "
                    f"writing as claude to /etc or mkdir under root-owned "
                    f"/var/log is still Permission denied)."
                )
        # sudoers (/etc) + timer (/etc) + service (/etc) + log dir (/var/log)
        # = four root-territory writes.
        assert found_root >= 4, (
            f"Expected >=4 root-owned writes in security_audit.py (the "
            f"/etc/sudoers.d sudoers rule, the /etc/systemd/system timer + "
            f"service units, and the /var/log/bubble-security log dir), "
            f"classifier found {found_root}."
        )

    def test_home_claude_writes_have_sudo_user_claude(self):
        tree = self._tree()
        env = _sa_assign_env(tree)
        found_claude = 0
        for qual, node in _sa_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _sa_str_fragments(kw.value, env)
            if _sa_classify(frags) == "claude":
                found_claude += 1
                assert _sa_has_kw(node, "_sudo"), (
                    f"security_audit: {qual} writing claude path "
                    f"{sorted(frags)} is MISSING _sudo=True."
                )
                assert _sa_kw_is_claude(node, "_sudo_user"), (
                    f"security_audit: {qual} writing claude path "
                    f"{sorted(frags)} must set _sudo_user=\"claude\"."
                )
        # /home/claude/scripts/ dir + security-audit.sh template = two writes.
        assert found_claude >= 2, (
            f"Expected >=2 claude-owned /home/claude writes (scripts dir + "
            f"security-audit.sh), classifier found {found_claude}."
        )

    def test_systemctl_shell_commands_have_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _sa_ops(tree):
            if qual != "server.shell":
                continue
            joined = "\n".join(_sa_command_strings(node))
            if "systemctl " in joined or "journalctl " in joined:
                seen += 1
                assert _sa_has_kw(node, "_sudo"), (
                    f"security_audit: server.shell running {joined!r} is "
                    f"MISSING _sudo=True — systemctl is root-only."
                )
                assert not _sa_kw_is_claude(node, "_sudo_user"), (
                    f"security_audit: server.shell running {joined!r} must "
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
        for qual, node in _sa_ops(tree):
            if qual != "systemd.service":
                continue
            seen += 1
            assert _sa_has_kw(node, "_sudo"), (
                "security_audit: systemd.service (enable/start the timer) "
                "is MISSING _sudo=True — root-only operation."
            )
            assert not _sa_kw_is_claude(node, "_sudo_user"), (
                "security_audit: systemd.service must NOT set "
                "_sudo_user=claude — systemctl enable/start is root-only."
            )
        assert seen >= 1, (
            "Expected a systemd.service op (enable+start the timer) in "
            "security_audit.py."
        )

    def test_log_dir_escalates_to_root_not_claude(self):
        """The /var/log/bubble-security log dir is created in root-owned
        /var/log/ (claude can't mkdir there). It must escalate to ROOT
        (`_sudo=True` ALONE) — NOT _sudo_user=claude, which would run
        `sudo -u claude mkdir /var/log/...` and hit Permission denied. The
        dir is owned root:adm (0750) on purpose: a compromised claude agent
        must NOT be able to read its own audit history."""
        tree = self._tree()
        env = _sa_assign_env(tree)
        log_nodes = []
        for qual, node in _sa_ops(tree):
            if qual != "files.directory":
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _sa_str_fragments(kw.value, env)
            if any("/var/log/bubble-security" in f for f in frags):
                log_nodes.append(node)
        assert len(log_nodes) == 1, (
            f"Expected exactly one files.directory for the /var/log/"
            f"bubble-security log dir, found {len(log_nodes)}."
        )
        node = log_nodes[0]
        assert _sa_has_kw(node, "_sudo"), (
            "security_audit: the /var/log/bubble-security files.directory is "
            "MISSING _sudo=True — /var/log is root-owned."
        )
        assert not _sa_has_kw(node, "_sudo_user"), (
            "security_audit: the /var/log/bubble-security files.directory "
            "must NOT set _sudo_user=claude — claude cannot mkdir under "
            "root-owned /var/log (and the dir is root:adm by design)."
        )
