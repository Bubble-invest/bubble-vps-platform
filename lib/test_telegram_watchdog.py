"""Static + golden-file tests for the Telegram plugin recovery watchdog
(Task D — SPEC-013).

Three surfaces covered:

    1. Plaintext-leak guards on the rendered watchdog bash script:
       - Known leaked-credential prefixes (Telegram bot id, sk-or-v1,
         sk-ant-oat01, tskey-auth-) MUST NOT appear in the rendered
         template. The script reads the token at runtime from the
         tmpfs env file — it never bakes a credential into the
         template.

    2. SPEC-008 hard rule extension to the watchdog script:
       - Every `curl ... bot${TOKEN}` invocation MUST be followed by
         `unset TOKEN` (or fall through to a clean exit path that
         unsets). This guarantees the token isn't lingering in the
         shell environment after the watchdog finishes its work.

    3. Golden-file compare for the .timer + .service systemd units —
       same approach as test_hardening_templates.py and the agent
       layer's test_systemd_unit_template_matches_golden.

    4. Static check on the pyinfra task module — must include a
       files.template (or files.put) targeting the sudoers drop-in
       path. Catches a refactor accidentally dropping the sudoers
       step (without which the watchdog can't restart the agent).

Run with: python3.12 -m pytest lib/test_telegram_watchdog.py -v
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


# Literal credential prefixes that MUST NOT leak into the rendered watchdog
# script. Same set as test_agent_layer.py + tskey-auth- (Tailscale auth key
# prefix — also lives in /run/claude-agent/env, so we want to be sure the
# watchdog doesn't accidentally splash it).
LEAKED_PREFIXES = (
    "8350575119:",   # Telegram bot id (rotated)
    "sk-or-v1-",     # OpenRouter key prefix
    "sk-ant-oat01-", # Anthropic OAuth token prefix
    "tskey-auth-",   # Tailscale auth key prefix
)


# Default render kwargs — matches what telegram_watchdog.apply() passes for
# bubble-internal. Keep these in sync if the module's defaults change.
_DEFAULT_RENDER_KWARGS = {
    # SPEC-001 v1.2 (multi-concierge): unit names are persona-suffixed.
    "unit_basename": "telegram-watchdog-morty",
    "persona_name": "morty",
    "service_name": "claude-agent-morty.service",
    # morty → bare telegram/ (the plugin's built-in default channel dir).
    "bot_pid_file": "/home/claude/.claude/channels/telegram/bot.pid",
    "decrypted_runtime_path": "/run/claude-agent/env",
    "cooldown_seconds": 300,
    "joris_telegram_user_id": "6532205130",
    "last_restart_mark": "/run/telegram-watchdog-morty/last-restart",
    # SPEC-021 FIX-4b: the agent's session-transcript dir (workdir / → -).
    "session_projects_dir": "/home/claude/.claude/projects/-home-claude-agents-morty",
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


# ─── 1. Watchdog script — no plaintext credentials ──────────────────────────


def test_watchdog_script_template_no_plaintext_secrets():
    """Render the bash template with mock cfg, grep for known leaked-credential
    prefixes. The watchdog reads the token at runtime from a tmpfs env file —
    it must NEVER bake a credential into the template itself.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    for prefix in LEAKED_PREFIXES:
        assert prefix not in rendered, (
            f"Leaked credential prefix {prefix!r} found in rendered "
            f"telegram-watchdog.sh — the script must read the token at "
            f"runtime from {_DEFAULT_RENDER_KWARGS['decrypted_runtime_path']}, "
            f"never embed it as a template literal."
        )


# ─── 2. Watchdog script — every TOKEN use is followed by unset ──────────────


def test_watchdog_script_unsets_token_after_use():
    """SPEC-008 hard rule extension: every `curl ... bot${TOKEN}` use must be
    followed by `unset TOKEN` before the script can exit any other path.

    Implementation: scan the rendered script line-by-line. For each line that
    references `${TOKEN}` in a curl URL, ensure SOME later line within the
    same execution branch contains `unset TOKEN`. We approximate "same branch"
    by requiring `unset TOKEN` to appear at least once after every
    `bot${TOKEN}/` reference, and that the count of `unset TOKEN` statements
    is >= the count of `bot${TOKEN}/` references.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # Count usages of the token in curl URLs.
    token_uses = re.findall(r"bot\$\{TOKEN\}/", rendered)
    unset_count = len(re.findall(r"\bunset\s+TOKEN\b", rendered))

    assert token_uses, (
        "Sanity check: the rendered watchdog script doesn't reference "
        "${TOKEN} at all in a curl URL. That means it's not actually "
        "talking to Telegram — broken script."
    )
    assert unset_count >= len(token_uses), (
        f"Watchdog script uses ${{TOKEN}} in {len(token_uses)} curl URL(s) "
        f"but only contains {unset_count} `unset TOKEN` statement(s). "
        f"SPEC-008 requires the token to be unset after every use so it "
        f"doesn't linger in the shell environment."
    )

    # Also: the LAST reference to ${TOKEN} must be followed by an `unset TOKEN`
    # before the next exit / end of script. Locate the last `${TOKEN}` use and
    # the last `unset TOKEN`; the unset must come after.
    last_use = max(m.start() for m in re.finditer(r"\$\{TOKEN\}", rendered))
    last_unset = max(
        (m.start() for m in re.finditer(r"\bunset\s+TOKEN\b", rendered)),
        default=-1,
    )
    assert last_unset > last_use, (
        "The last `${TOKEN}` reference in the watchdog script is NOT "
        "followed by an `unset TOKEN`. The token would linger in the "
        "shell env until the process exits — defense-in-depth violation."
    )


def test_watchdog_script_uses_token_via_https_only():
    """Belt-and-suspenders: the only place `${TOKEN}` appears in a URL must
    be HTTPS to api.telegram.org. No HTTP curl, no echo, no file write.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    token_url_lines = [
        line for line in rendered.splitlines()
        if "${TOKEN}" in line and ("curl" in line or "wget" in line or "https://" in line or "http://" in line)
    ]
    for line in token_url_lines:
        assert "https://api.telegram.org" in line, (
            f"Watchdog script uses ${{TOKEN}} on a non-HTTPS-Telegram URL: "
            f"{line!r}. Tokens must only flow over HTTPS to Telegram."
        )
        assert "http://" not in line.replace("https://", ""), (
            f"Watchdog script has a plaintext-HTTP curl with the token: {line!r}"
        )


# ─── 2b. Watchdog script — Signal 5: wedged-bridge detection ────────────────


def test_watchdog_detects_wedged_bridge_signal5():
    """SPEC-021 FIX-5 (2026-06-01 Morty outage): the watchdog must detect the
    'armed but deaf' wedge — the plugin is up (bot.pid alive, bun poller in
    cgroup, pending<=5) yet the session never ingests messages.

    Fingerprint of that incident: a non-watchdog `systemctl restart` left the
    SDK's poller->session bridge wedged. The poller DRAINED updates (pending
    returned to 0) but NO session transcript was written for many minutes. All
    four prior signals passed, so the watchdog said 'ok' while Morty was deaf.

    The detection compares the plugin's arm time (bot.pid mtime) against the
    newest session-transcript mtime, using a persisted last-seen-pending state
    file to confirm the poller actually consumed traffic since arming. We assert
    the rendered script wires this up: it references the bot.pid mtime, the
    session transcript mtime, a wedge staleness threshold, and sets broken=1
    with a 'wedge' reason.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # The wedge block must exist and key off transcript staleness vs arm time.
    assert "WEDGE_STALE_SEC" in rendered, (
        "Signal 5 missing: no WEDGE_STALE_SEC threshold in the watchdog. The "
        "wedged-bridge detection (2026-06-01 Morty outage) is not implemented."
    )
    assert "bot.pid" in rendered and "stat -c %Y" in rendered, (
        "Signal 5 must compare bot.pid mtime (plugin arm time) against the "
        "newest session transcript mtime via `stat -c %Y`."
    )
    # Must set broken with a recognisable wedge reason so the recovery path
    # (stop->start, which clears any zombie poller) fires.
    assert re.search(r'broken=1\s*;?\s*\n?\s*reason="wedge', rendered), (
        "Signal 5 must set broken=1 with a reason starting 'wedge...' so the "
        "existing clean stop->start recovery path runs."
    )
    # False-positive guard: the wedge check must only run when the plugin has
    # actually been armed a while (not a fresh boot) — assert it references a
    # grace/min-arm period so an idle-since-boot agent isn't flagged.
    assert "WEDGE_MIN_ARM_SEC" in rendered, (
        "Signal 5 needs a WEDGE_MIN_ARM_SEC grace so a freshly (re)started but "
        "legitimately idle agent is not falsely flagged as wedged."
    )


def test_watchdog_signal5_state_file_is_runtime_tmpfs():
    """The persisted last-seen-pending state used by Signal 5 must live under
    /run (tmpfs, per-boot) like the other watchdog state, not in a repo or home
    path — so it resets cleanly on reboot and never accumulates."""
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Find the wedge state file var assignment; it must be under /run.
    m = re.search(r'WEDGE_STATE[A-Z_]*=("?)(/[^"\s]+)', rendered)
    assert m, "Signal 5 state-file variable not found in watchdog script."
    assert m.group(2).startswith("/run/"), (
        f"Signal 5 state file is at {m.group(2)!r} — must be under /run (tmpfs)."
    )


# ─── 2c. Watchdog script — Signal 6: deaf-drop (unanswered enqueue) ─────────


def test_watchdog_detects_unanswered_enqueue_signal6():
    """SPEC-021 FIX-6 (2026-06-01 Tony deaf incident + #61797): the watchdog must
    detect the CC-core notification DROP — an inbound was enqueued into the
    session jsonl (a `queue-operation`/`<channel ...>` line) but NO assistant
    turn was ever written after it, and it's been stale for > a grace window.

    This is the failure Signal 5 misses: the poller drains so fast that
    pending is ~always 0 at tick time, so Signal 5's `prev_pending>0` gate never
    trips. Signal 6 reads the transcript DIRECTLY: last enqueue with no later
    assistant turn = deaf, regardless of pending. Decisive evidence: Tony's
    session 8ea35072 — inbound enqueued 13:40, never dequeued, 0 mcp/subagent
    activity.

    We assert the rendered script wires this up: a DEAF grace threshold, a scan
    of the newest transcript for an enqueue/channel marker with no assistant
    line after it, and broken=1 with a 'deaf' reason so the recovery path fires.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)

    assert "DEAF_GRACE_SEC" in rendered, (
        "Signal 6 missing: no DEAF_GRACE_SEC threshold. The unanswered-enqueue "
        "(CC-core notification drop / #61797) detection is not implemented."
    )
    # Must look for the enqueue / inbound channel marker in the transcript.
    assert "queue-operation" in rendered or "enqueue" in rendered, (
        "Signal 6 must scan the session transcript for the enqueue marker "
        "(queue-operation/enqueue line) that proves an inbound landed."
    )
    # Must key off 'no assistant turn after the enqueue' and set a deaf reason.
    assert re.search(r'broken=1\s*;?\s*\n?\s*reason="deaf', rendered), (
        "Signal 6 must set broken=1 with a reason starting 'deaf...' so the "
        "recovery path runs when an enqueue went unanswered."
    )


def test_watchdog_signal6_busy_guard_skips_subagent_and_tool_work():
    """Regression for the 2026-06-02 Morty restart loop: Signal 6 must NOT flag
    a legitimately-long turn (one driven by subagents) as deaf. Such a turn
    writes NOTHING to the MAIN transcript after the enqueue until it completes —
    work lands in SESSION/<id>/subagents/*.jsonl and tool_use/tool_result lines
    accrue — so the bare 'no assistant after enqueue' check false-positives,
    the watchdog restarts mid-turn, and the --continue resume RE-LAUNCHES the
    same long task → infinite loop.

    We assert the rendered script gates the deaf verdict on a BUSY check:
      (a) an active subagent transcript within the grace window, AND/OR
      (b) tool_use/tool_result activity after the enqueue,
    and only sets broken=1 when NOT busy. We also assert the grace window is no
    longer the too-short 120s that tripped on Opus turns.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)

    # (a) subagent-activity guard: scans the per-session subagents/ dir for a
    # recently-written transcript.
    assert "subagents" in rendered and "-newermt" in rendered, (
        "Signal 6 busy-guard missing: must check for an active subagent "
        "transcript (SESSION/<id>/subagents/*.jsonl written within the grace "
        "window) before declaring the turn deaf."
    )
    # (b) tool-activity guard: tool_use / tool_result after the enqueue line.
    assert "tool_(use|result)" in rendered or "tool_use" in rendered, (
        "Signal 6 busy-guard missing: must treat tool_use/tool_result activity "
        "after the enqueue as a live turn (not deaf)."
    )
    # The deaf verdict must require busy==0 (the turn is genuinely idle).
    assert re.search(r'busy\s*==\s*0', rendered), (
        "Signal 6 must only set the deaf verdict when busy==0; otherwise a "
        "subagent/tool-driven turn restart-loops (Morty 2026-06-02)."
    )
    # Grace must be well above the old 120s that tripped on long Opus turns.
    m = re.search(r"DEAF_GRACE_SEC=(\d+)", rendered)
    assert m and int(m.group(1)) >= 300, (
        "DEAF_GRACE_SEC must be >=300s — 120s was far too short for Opus turns "
        "and caused the 2026-06-02 restart loop."
    )


# ─── 2d. Watchdog recovery — resume session with --continue --fork-session ──


def test_watchdog_recovery_resumes_session_with_continue():
    """Joris directive 2026-06-01: the auto-restart recovery must RESUME the
    prior session (preserve context) via `--continue --fork-session`, not start
    blank. Mechanism (HARDENED per Codex P1 review): the watchdog calls a
    ROOT-OWNED helper that installs/removes a transient drop-in with FIXED
    content — NOT a raw `sudo tee` (which would let a compromised `claude`
    account write arbitrary `[Service]` content and escalate to root).

    We assert the rendered script: invokes the helper for install AND remove,
    and does NOT use raw `sudo tee`/`sudo mkdir`/`sudo rm` against the drop-in.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)

    assert "bubble-watchdog-resume-dropin install" in rendered, (
        "Recovery must install the transient resume drop-in via the root-owned "
        "helper (fixed content), not raw tee."
    )
    assert "bubble-watchdog-resume-dropin remove" in rendered, (
        "Recovery must remove the transient resume drop-in via the helper after "
        "restart so a normal (fresh) boot isn't permanently switched to --continue."
    )
    # SECURITY regression guard: the script must NOT pipe arbitrary content into
    # a sudo'd tee/dd against the systemd drop-in (the Codex P1 escalation path).
    assert "sudo /usr/bin/tee" not in rendered, (
        "Recovery must NOT use `sudo tee` to write the drop-in — that lets a "
        "compromised claude account write arbitrary unit content and escalate "
        "to root (Codex P1). Use the fixed-content root helper instead."
    )


def test_resume_dropin_helper_writes_fixed_content_and_allowlists():
    """The root-owned helper that writes the resume drop-in must (a) hardcode the
    drop-in content (no stdin / no caller-controlled bytes) and (b) validate the
    service name against an allowlist so it can't be steered to an arbitrary unit.
    Closes the Codex P1 root-escalation finding.
    """
    rendered = _render(
        "bubble-watchdog-resume-dropin.sh.j2",
        service_name="ops-loop-tony.service",  # unused by the template, kept for parity
    )
    # (a) fixed content: a heredoc writing the [Service] block, no `$(...)`/stdin
    #     interpolation of caller input into the unit file.
    assert "ExecStart=/bin/sh -c" in rendered and "--continue --fork-session" in rendered, (
        "Helper must write the fixed resume ExecStart content itself."
    )
    # (b) allowlist on the service name argument.
    assert re.search(r'ops-loop-\[a-z0-9\]\+|claude-agent-\[a-z0-9\]\+', rendered), (
        "Helper must validate the service-name argument against an allowlist "
        "regex so it can't be pointed at an arbitrary systemd unit."
    )
    # The helper itself does the daemon-reload (so the sudoers grant doesn't need
    # a bare daemon-reload).
    assert "systemctl daemon-reload" in rendered, (
        "Helper must run its own daemon-reload after install/remove."
    )


def test_sudoers_does_not_grant_arbitrary_dropin_write():
    """SECURITY regression guard (Codex P1): the sudoers rule must NOT grant the
    claude user raw `tee`/`mkdir`/`rm` against the systemd drop-in path, nor a
    bare `daemon-reload`. It may only grant the fixed-content helper verbs.
    """
    rendered = _render("sudoers-telegram-watchdog.j2", **{
        "unit_basename": "telegram-watchdog-tony",
        "service_name": "ops-loop-tony.service",
    })
    grant_line = [l for l in rendered.splitlines() if l.startswith("claude ")]
    assert grant_line, "no claude grant line in rendered sudoers"
    line = grant_line[0]
    for forbidden in ("/usr/bin/tee", "/usr/bin/mkdir", "/usr/bin/rm",
                      "systemctl daemon-reload"):
        assert forbidden not in line, (
            f"sudoers grants `{forbidden}` to claude — this is the Codex P1 "
            f"root-escalation vector. Only the fixed-content helper may be granted."
        )
    assert "bubble-watchdog-resume-dropin install" in line and \
           "bubble-watchdog-resume-dropin remove" in line, (
        "sudoers must grant the resume-dropin helper verbs (install/remove)."
    )


# ─── 3. Golden-file compare for systemd timer + service ─────────────────────


def test_watchdog_systemd_timer_renders():
    """Render telegram-watchdog.timer.j2 for morty (persona-suffixed); compare
    to the committed golden (what pyinfra writes to /etc/systemd/system/).
    """
    rendered = _render(
        "telegram-watchdog.timer.j2",
        unit_basename="telegram-watchdog-morty",
        persona_name="morty",
    )
    expected = _golden("telegram-watchdog-morty.timer")
    assert rendered == expected, (
        f"telegram-watchdog.timer template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_watchdog_systemd_service_renders():
    """Render telegram-watchdog.service.j2 for morty; compare to golden."""
    rendered = _render(
        "telegram-watchdog.service.j2",
        unit_basename="telegram-watchdog-morty",
        persona_name="morty",
    )
    expected = _golden("telegram-watchdog-morty.service")
    assert rendered == expected, (
        f"telegram-watchdog.service template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


# ─── 4. pyinfra task module — drops the sudoers rule ────────────────────────


def test_watchdog_pyinfra_module_drops_sudoers():
    """Read the pyinfra task module source, assert it includes a
    `files.template` (or `files.put`) targeting
    /etc/sudoers.d/claude-telegram-watchdog. Without this drop, the
    watchdog has no NOPASSWD privilege to restart the agent service.
    """
    module_path = ACCESS_TASKS_DIR / "telegram_watchdog.py"
    assert module_path.is_file(), (
        f"pyinfra task module missing: {module_path}. "
        f"This is the entry point for SPEC-013."
    )
    source = module_path.read_text(encoding="utf-8")

    # Must reference the persona-suffixed sudoers path (SPEC-001 v1.2:
    # /etc/sudoers.d/claude-telegram-watchdog-<name>). The path is built from
    # unit_basename = "telegram-watchdog-<name>", so we assert the module
    # constructs the `claude-{unit_basename}` drop-in path.
    assert "/etc/sudoers.d/claude-" in source and "unit_basename" in source, (
        "telegram_watchdog.py does not build the per-concierge sudoers drop-in "
        "path /etc/sudoers.d/claude-<unit_basename>. Without this drop, the "
        "watchdog can't restart claude-agent-<persona>.service when it "
        "detects a broken plugin."
    )

    # Must invoke files.template or files.put (the two pyinfra ops that
    # ship file contents to the box).
    assert re.search(r"files\.(template|put)\s*\(", source), (
        "telegram_watchdog.py doesn't use files.template or files.put — "
        "the sudoers rule never reaches the box."
    )

    # Sanity: the sudoers template itself must exist.
    sudoers_template = TEMPLATES_DIR / "sudoers-telegram-watchdog.j2"
    assert sudoers_template.is_file(), (
        f"sudoers template missing at {sudoers_template}. "
        f"Even if telegram_watchdog.py references the path, without the "
        f"template file pyinfra will fail at render-time."
    )


def test_watchdog_sudoers_template_well_formed():
    """Sanity-check the rendered sudoers content: single NOPASSWD rule line
    targeting the claude user + the two systemctl operations on the agent
    service. No shell metacharacters. This is the static guardrail for the
    visudo-validation gotcha documented in the module docstring.
    """
    rendered = _render(
        "sudoers-telegram-watchdog.j2",
        service_name="claude-agent-morty.service",
        unit_basename="telegram-watchdog-morty",
    )
    # Must contain the canonical NOPASSWD line.
    assert "claude ALL=(ALL) NOPASSWD:" in rendered, (
        "Sudoers template missing the canonical NOPASSWD rule prefix."
    )
    # SPEC-021 FIX-4a: recovery is now stop→start, so the rule MUST grant stop
    # + start. restart + is-active are retained for backward-compat / probes.
    assert "/usr/bin/systemctl stop claude-agent-morty.service" in rendered, (
        "Sudoers must permit `systemctl stop` — recovery now uses stop→start."
    )
    assert "/usr/bin/systemctl start claude-agent-morty.service" in rendered, (
        "Sudoers must permit `systemctl start` — recovery now uses stop→start."
    )
    assert "/usr/bin/systemctl restart claude-agent-morty.service" in rendered
    assert "/usr/bin/systemctl is-active claude-agent-morty.service" in rendered
    # No shell metacharacters that would let the watchdog escape the
    # sudoers scope (e.g. `;`, `&&`, `|`, `$`, backticks).
    rule_lines = [
        line for line in rendered.splitlines()
        if line and not line.lstrip().startswith("#")
    ]
    for line in rule_lines:
        for bad in (";", "&&", "||", "`", "$("):
            assert bad not in line, (
                f"Sudoers rule contains shell metacharacter {bad!r} which "
                f"would allow privilege escalation beyond the intended scope: "
                f"{line!r}"
            )


# ─── 5. Watchdog script — checks all four SPEC-013 detection signals ───────


def test_watchdog_script_checks_all_detection_signals():
    """Per SPEC-013 §"Detection signals" (extended by SPEC-021), the watchdog
    must check 4 signals: (1) bot.pid missing, (2) PID dead, (3) no bun poller
    in THIS service's cgroup, (4) pending count. Catch a refactor that drops
    one of them.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Signal 1: bot.pid existence
    assert '! -f "$BOT_PID_FILE"' in rendered or "! -f \"$BOT_PID_FILE\"" in rendered, (
        "Watchdog missing signal #1 (bot.pid missing)"
    )
    # Signal 2: PID alive
    assert "kill -0" in rendered, (
        "Watchdog missing signal #2 (kill -0 to test PID liveness)"
    )
    # Signal 3: bun poller check — SPEC-021 FIX-3 requires this be CGROUP-SCOPED
    # (was a bare `pgrep -f "bun run.*telegram"` that false-negatived on a
    # multi-agent box). Assert the cgroup-scoped helper is present.
    assert "is_bun_telegram_alive_in_cgroup" in rendered, (
        "Watchdog missing signal #3 (cgroup-scoped bun poller liveness check)"
    )
    # Signal 4: pending update count
    assert "getWebhookInfo" in rendered, (
        "Watchdog missing signal #4 (getWebhookInfo / pending_update_count)"
    )
    assert "pending_update_count" in rendered, (
        "Watchdog should reference pending_update_count from getWebhookInfo"
    )


def test_watchdog_bun_check_is_cgroup_scoped_not_broad_pgrep():
    """SPEC-021 FIX-3: the bun-poller liveness check MUST be scoped to THIS
    service's cgroup, NOT a fleet-wide `pgrep -f "bun run.*telegram"` that
    matches ANY agent's poller (a false negative on a multi-agent box that
    leaves a dead agent unrecovered).

    Assert: (a) the broad unscoped pattern is gone, and (b) the check consults
    the service's cgroup (via systemctl MainPID + /proc/<pid>/cgroup).
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert 'pgrep -f "bun run.*telegram"' not in rendered, (
        "Watchdog still uses the broad unscoped `pgrep -f \"bun run.*telegram\"` "
        "— on a multi-agent box this matches other agents' pollers and returns "
        "a false 'alive' for a dead agent (FIX-3)."
    )
    assert "/proc/" in rendered and "cgroup" in rendered, (
        "Watchdog bun-poller check must consult /proc/<pid>/cgroup to scope to "
        "THIS service. Missing the cgroup-scoped check."
    )
    assert "MainPID" in rendered, (
        "Watchdog must query `systemctl show $SERVICE -p MainPID` to anchor the "
        "cgroup-scoped liveness check on systemd's own process accounting."
    )


def test_watchdog_recovery_uses_stop_start_not_restart():
    """SPEC-021 FIX-4a: recovery must be stop → settle → rm bot.pid → start,
    NOT a bare `systemctl restart` (which leaves a zombie bun poller holding
    the getUpdates slot, so the agent comes back active-but-deaf — the exact
    state the 2026-05-31 outage was only cleared by a full stop→start).
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Stop + start present, in that order, in the recovery path.
    stop_pos = rendered.find("systemctl stop \"$SERVICE\"")
    start_pos = rendered.find("systemctl start \"$SERVICE\"")
    assert stop_pos != -1, (
        "Watchdog recovery must `systemctl stop $SERVICE` (FIX-4a)."
    )
    assert start_pos != -1, (
        "Watchdog recovery must `systemctl start $SERVICE` (FIX-4a)."
    )
    assert stop_pos < start_pos, (
        "Watchdog recovery must stop BEFORE start — found start before stop."
    )
    # The stale bot.pid must be removed between stop and start so the next
    # health check can't be fooled by a leftover file.
    rm_pos = rendered.find('rm -f "$BOT_PID_FILE"')
    assert stop_pos < rm_pos < start_pos, (
        "Watchdog must `rm -f $BOT_PID_FILE` between stop and start (FIX-4a)."
    )
    # The recovery path must NOT issue a bare `systemctl restart $SERVICE`
    # (the old behavior). `restart` may still appear in comments/sudoers, but
    # not as an executed recovery command.
    assert 'systemctl restart "$SERVICE"' not in rendered, (
        "Watchdog still issues `systemctl restart $SERVICE` — replace with "
        "stop→start to avoid the zombie poller (FIX-4a)."
    )


def test_watchdog_detects_401_and_alerts_without_restarting():
    """SPEC-021 FIX-4b: before restarting, the watchdog must tail this agent's
    newest session jsonl and, on finding a 401 auth failure (or "Please run
    /login"), send a DISTINCT operator alert and exit WITHOUT restarting (a
    restart can't fix bad creds and would loop / thrash the whole fleet).
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    # Reads the session transcript dir.
    assert "SESSION_PROJECTS_DIR" in rendered, (
        "Watchdog must reference the agent's session-transcript dir for the "
        "401 probe (FIX-4b)."
    )
    # Greps for the auth-failure fingerprints.
    assert "401 Invalid authentication credentials" in rendered, (
        "Watchdog 401 probe must match the '401 Invalid authentication "
        "credentials' fingerprint."
    )
    assert "Please run /login" in rendered, (
        "Watchdog 401 probe must also match 'Please run /login'."
    )
    # The 401 branch must come BEFORE the recovery (stop) — restart must not
    # fire on an auth failure.
    auth_pos = rendered.find("AUTH 401 detected")
    stop_pos = rendered.find("systemctl stop \"$SERVICE\"")
    assert auth_pos != -1, "Watchdog missing the AUTH 401 detection branch."
    assert auth_pos < stop_pos, (
        "The AUTH 401 branch must be evaluated BEFORE the stop→start recovery "
        "so a 401 exits without restarting (FIX-4b)."
    )
    # The 401 branch must exit (not fall through into the restart path).
    auth_block = rendered[auth_pos:stop_pos]
    assert "exit 1" in auth_block, (
        "The AUTH 401 branch must exit before reaching the restart path."
    )
    # The 401 alert message must signal that re-auth is needed.
    assert "needs re-auth" in rendered or "AUTH 401" in rendered, (
        "The 401 alert must tell the operator the agent needs re-auth."
    )


def test_watchdog_401_alert_respects_cooldown():
    """SPEC-021 FIX-4b: a persistent 401 must alert at most once per cooldown
    window (don't spam). The 401 branch must consult the same LAST_RESTART_MARK
    cooldown the restart path uses.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    auth_pos = rendered.find("AUTH 401 detected")
    # Grab the 401 branch region (from the auth_failed gate to the recovery).
    auth_gate = rendered.find("if [[ $auth_failed -eq 1 ]]")
    stop_pos = rendered.find("systemctl stop \"$SERVICE\"")
    assert auth_gate != -1 and stop_pos != -1
    auth_region = rendered[auth_gate:stop_pos]
    assert "COOLDOWN_SECONDS" in auth_region, (
        "The 401 branch must reference COOLDOWN_SECONDS to rate-limit alerts."
    )
    assert "LAST_RESTART_MARK" in auth_region, (
        "The 401 branch must consult LAST_RESTART_MARK for its cooldown."
    )


def test_watchdog_script_has_cooldown_gate():
    """Per SPEC-013, the watchdog must NOT restart in a loop — one attempt
    per cooldown window. Verify the cooldown gate is present.
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "COOLDOWN_SECONDS" in rendered
    assert "LAST_RESTART_MARK" in rendered or "/run/telegram-watchdog/" in rendered
    # Conditional check on cooldown
    assert re.search(r"now\s*-\s*last\s*<\s*COOLDOWN_SECONDS", rendered), (
        "Watchdog missing the (now - last < COOLDOWN_SECONDS) cooldown check"
    )


def test_watchdog_script_uses_set_uo_pipefail_not_e():
    """Per SPEC-013: `set -uo pipefail` (NOT -e — we want to handle each
    check individually, not abort on the first non-zero exit).
    """
    rendered = _render("telegram-watchdog.sh.j2", **_DEFAULT_RENDER_KWARGS)
    assert "set -uo pipefail" in rendered, (
        "Watchdog script must `set -uo pipefail` (NOT -e); see SPEC-013 "
        "rationale for why -e is wrong here."
    )


# ─── 6. pyinfra task module — orchestration wiring ──────────────────────────


def test_top_level_deploy_imports_telegram_watchdog():
    """deploy.py must import the watchdog task module and call its apply()
    after tailscale.apply(). Order: hardening → secrets → agent → tailscale
    → telegram_watchdog → hello.
    """
    deploy_path = REPO_ROOT / "deploy.py"
    source = deploy_path.read_text(encoding="utf-8")
    assert "telegram_watchdog" in source, (
        "Top-level deploy.py does not reference telegram_watchdog — the "
        "Task D module is never invoked at deploy time."
    )
    assert re.search(r"telegram_watchdog\.apply\s*\(", source), (
        "Top-level deploy.py never calls telegram_watchdog.apply()."
    )
    # Must come AFTER tailscale.apply()
    tailscale_pos = source.find("tailscale.apply")
    watchdog_pos = source.find("telegram_watchdog.apply")
    assert tailscale_pos != -1 and watchdog_pos != -1, (
        "Both tailscale.apply and telegram_watchdog.apply must be present"
    )
    assert tailscale_pos < watchdog_pos, (
        f"telegram_watchdog.apply (pos {watchdog_pos}) is invoked BEFORE "
        f"tailscale.apply (pos {tailscale_pos}). Per SPEC-013 ordering, "
        f"tailscale must come first."
    )


def test_pyinfra_module_uses_get_tenant_config():
    """Same architectural invariant as the secrets + agent layers: read tenant
    config via lib.host_helpers.get_tenant_config, never parse YAML directly."""
    module_path = ACCESS_TASKS_DIR / "telegram_watchdog.py"
    source = module_path.read_text(encoding="utf-8")
    # Allow either a single-line import or a parenthesized multi-line import
    # block (the module imports several helpers from lib.host_helpers now that
    # multi-concierge derivations live there). DOTALL so the import list can
    # span lines; bound to the host_helpers import statement only.
    assert re.search(
        r"from\s+lib\.host_helpers\s+import\s*\(?[^)]*\bget_tenant_config\b",
        source,
        re.DOTALL,
    ), (
        "telegram_watchdog.py must import get_tenant_config from "
        "lib.host_helpers — direct YAML parsing in pyinfra tasks bypasses "
        "the SPEC-003 host-data exposure policy."
    )


# ─── 7. Per-persona channel-dir derivation (SPEC-021 FIX-2) ──────────────────


class TestPerPersonaChannelDir:
    """The watchdog's bot.pid path MUST be per-persona, derived from the SAME
    single source the plugin uses for its state dir. morty keeps the bare
    `telegram/` (the plugin's built-in default); other departments get
    `telegram-<persona>/`. A mismatch is the FIX-2 bug — the watchdog reads the
    wrong agent's liveness marker on a multi-agent box.
    """

    def test_helper_morty_is_bare_telegram(self):
        from lib.host_helpers import (
            telegram_bot_pid_file,
            telegram_channel_dir,
            telegram_channel_dir_name,
        )

        assert telegram_channel_dir_name("morty") == "telegram", (
            "morty must keep the bare `telegram/` channel dir (the plugin's "
            "built-in default) — that's where its bot.pid actually lives."
        )
        assert telegram_channel_dir("morty") == (
            "/home/claude/.claude/channels/telegram"
        )
        assert telegram_bot_pid_file("morty") == (
            "/home/claude/.claude/channels/telegram/bot.pid"
        )

    def test_helper_other_persona_is_suffixed(self):
        from lib.host_helpers import (
            telegram_bot_pid_file,
            telegram_channel_dir_name,
        )

        # A future department (e.g. maya) must get an isolated suffixed dir so
        # its poller doesn't fight morty's over the same getUpdates slot.
        assert telegram_channel_dir_name("maya") == "telegram-maya"
        assert telegram_bot_pid_file("maya") == (
            "/home/claude/.claude/channels/telegram-maya/bot.pid"
        )
        assert telegram_bot_pid_file("cgp") == (
            "/home/claude/.claude/channels/telegram-cgp/bot.pid"
        )

    def test_watchdog_module_uses_per_persona_bot_pid(self):
        """telegram_watchdog.py must derive bot.pid from the per-persona helper
        — NOT from a hardcoded `.../channels/telegram/bot.pid` constant (the
        old morty-only path that broke every other department's watchdog)."""
        source = (ACCESS_TASKS_DIR / "telegram_watchdog.py").read_text(
            encoding="utf-8"
        )
        assert "telegram_bot_pid_file(" in source, (
            "telegram_watchdog.py must call telegram_bot_pid_file(persona_name) "
            "to derive the per-persona bot.pid path (FIX-2)."
        )
        # The old hardcoded module-level constant assignment must be gone.
        assert (
            '_BOT_PID_FILE = "/home/claude/.claude/channels/telegram/bot.pid"'
            not in source
        ), (
            "telegram_watchdog.py still hardcodes the morty-only bot.pid path "
            "as a module constant — every other department's watchdog would "
            "read morty's liveness marker (FIX-2)."
        )

    def test_plugin_state_dir_uses_same_helper(self):
        """_telegram_plugin.py (which creates the channel dir the plugin writes
        bot.pid into) must use the SAME helper, so the created dir and the
        watched dir can never diverge (FIX-2 single-source invariant)."""
        source = (
            REPO_ROOT / "pyinfra" / "tasks" / "agent" / "_telegram_plugin.py"
        ).read_text(encoding="utf-8")
        assert "telegram_channel_dir(" in source, (
            "_telegram_plugin.py must derive its channel state dir from "
            "telegram_channel_dir(persona_name) — the same single source the "
            "watchdog uses for bot.pid."
        )

    def test_watchdog_renders_correct_bot_pid_for_named_persona(self):
        """End-to-end render check: a watchdog script rendered for persona X
        must reference X's channel dir in BOT_PID_FILE (not morty's)."""
        from lib.host_helpers import telegram_bot_pid_file

        kwargs = dict(_DEFAULT_RENDER_KWARGS)
        kwargs["service_name"] = "claude-agent-maya.service"
        kwargs["bot_pid_file"] = telegram_bot_pid_file("maya")
        kwargs["session_projects_dir"] = (
            "/home/claude/.claude/projects/-home-claude-agents-maya"
        )
        rendered = _render("telegram-watchdog.sh.j2", **kwargs)
        assert (
            'BOT_PID_FILE="/home/claude/.claude/channels/telegram-maya/bot.pid"'
            in rendered
        ), (
            "Watchdog rendered for persona 'maya' must point BOT_PID_FILE at "
            "telegram-maya/bot.pid, not the bare telegram/ dir."
        )


# ─── 8. Multi-concierge: persona-suffixed watchdog units (SPEC-001 v1.2) ──────


class TestMultiConciergeWatchdogSuffixing:
    """On a multi-concierge box every watchdog artifact MUST be persona-suffixed
    so two concierges (morty + claudette) never collide on unit names, script
    path, sudoers drop-in, or RuntimeDirectory. We chose MIGRATION-B: ALL
    concierges are suffixed uniformly (including morty)."""

    def test_unit_basename_helper_always_suffixed(self):
        from lib.host_helpers import watchdog_unit_basename

        assert watchdog_unit_basename("morty") == "telegram-watchdog-morty"
        assert watchdog_unit_basename("claudette") == "telegram-watchdog-claudette"
        # Even the primary/original concierge is suffixed (migration-b).
        assert watchdog_unit_basename("sandra") == "telegram-watchdog-sandra"

    def test_no_unit_name_collision_between_concierges(self):
        """Two distinct concierge names MUST yield distinct unit basenames (and
        therefore distinct timer/service/sudoers/runtime-dir paths)."""
        from lib.host_helpers import watchdog_unit_basename

        names = ["morty", "claudette", "sandra", "karl"]
        basenames = [watchdog_unit_basename(n) for n in names]
        assert len(set(basenames)) == len(names), (
            "watchdog_unit_basename produced a collision across concierge "
            "names — two concierges would fight over the same systemd units."
        )

    def test_timer_unit_field_is_suffixed(self):
        """The .timer's `Unit=` must point at the SUFFIXED .service so a
        concierge's timer triggers ITS OWN watchdog, not another's."""
        rendered = _render(
            "telegram-watchdog.timer.j2",
            unit_basename="telegram-watchdog-claudette",
            persona_name="claudette",
        )
        assert "Unit=telegram-watchdog-claudette.service" in rendered
        # The bare un-suffixed unit must NOT appear (would cross-trigger).
        assert "Unit=telegram-watchdog.service" not in rendered

    def test_service_runtime_dir_and_execstart_are_suffixed(self):
        """The .service RuntimeDirectory + ExecStart path must be suffixed so
        two concierges don't share /run/telegram-watchdog/ or the same script."""
        rendered = _render(
            "telegram-watchdog.service.j2",
            unit_basename="telegram-watchdog-claudette",
            persona_name="claudette",
        )
        assert "RuntimeDirectory=telegram-watchdog-claudette" in rendered
        assert (
            "ExecStart=/home/claude/scripts/telegram-watchdog-claudette.sh"
            in rendered
        )

    def test_sudoers_path_and_rule_are_per_concierge(self):
        """Each concierge's sudoers rule is pinned to ITS OWN agent service —
        no wildcard, no cross-grant."""
        rendered = _render(
            "sudoers-telegram-watchdog.j2",
            service_name="claude-agent-claudette.service",
            unit_basename="telegram-watchdog-claudette",
        )
        assert "/etc/sudoers.d/claude-telegram-watchdog-claudette" in rendered
        assert "/usr/bin/systemctl stop claude-agent-claudette.service" in rendered
        # Must NOT grant control over morty's service.
        assert "claude-agent-morty.service" not in rendered

    def test_watchdog_module_loops_over_concierges(self):
        """The pyinfra module must iterate cfg.agent.concierges and derive the
        suffixed unit basename + per-concierge runtime env file."""
        source = (ACCESS_TASKS_DIR / "telegram_watchdog.py").read_text(
            encoding="utf-8"
        )
        assert "cfg.agent.concierges" in source, (
            "telegram_watchdog.py must loop over cfg.agent.concierges — a "
            "single-persona deploy leaves additional concierges unwatched."
        )
        assert "watchdog_unit_basename(" in source, (
            "telegram_watchdog.py must derive the suffixed unit basename from "
            "watchdog_unit_basename(persona_name)."
        )
        assert "runtime_env_file(" in source, (
            "telegram_watchdog.py must read each concierge's OWN decrypted "
            "runtime env file via runtime_env_file()."
        )

    def test_claudette_runtime_env_matches_hand_deploy(self):
        """The non-primary concierge's runtime env file must match claudette's
        live hand-deploy path so the source refactor cuts over cleanly."""
        from lib.host_helpers import runtime_env_file

        path = runtime_env_file(
            "claudette", is_primary=False, primary_runtime_path="/run/claude-agent/env"
        )
        assert path == "/run/claude-agent-claudette/env", (
            "Non-primary concierge runtime env must be /run/claude-agent-<name>/env "
            "(matches claudette's interim hand-deploy)."
        )

    def test_primary_runtime_env_unchanged(self):
        """The PRIMARY concierge keeps the historical /run/claude-agent/env so
        morty's live unit does not churn on the cutover deploy."""
        from lib.host_helpers import runtime_env_file

        path = runtime_env_file(
            "morty", is_primary=True, primary_runtime_path="/run/claude-agent/env"
        )
        assert path == "/run/claude-agent/env"


# ─── Sudo-escalation static guard (regression: 2026-05-31 deploy blocker) ─────
#
# The deploy connects AS claude (tenant ssh_user: claude). The watchdog stack
# writes BOTH root-owned files (/etc/sudoers.d, /etc/systemd/system) AND a
# claude-owned script (/home/claude/scripts/...), and runs root-only systemctl
# commands. Each must escalate correctly or pyinfra dies with
# `[Errno 13] Permission denied` → `No hosts remaining!`. These AST tests pin
# the escalation kwargs so a refactor can't silently drop them.
#
# RULE: root target (/etc) → `_sudo=True` ALONE; claude target (/home/claude)
#       → `_sudo=True, _sudo_user="claude"`; root-only command → `_sudo=True`.


def _wd_assign_env(tree):
    import ast

    env: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(n.value)
    return env


def _wd_str_fragments(node, env, _depth=0):
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
                out |= _wd_str_fragments(v.value, env, _depth + 1)
    elif isinstance(node, ast.Name):
        for val in env.get(node.id, []):
            out |= _wd_str_fragments(val, env, _depth + 1)
    elif isinstance(node, ast.BinOp):
        out |= _wd_str_fragments(node.left, env, _depth + 1)
        out |= _wd_str_fragments(node.right, env, _depth + 1)
    return out


def _wd_has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def _wd_kw_is_claude(node, name):
    import ast

    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value == "claude"
    return False


def _wd_classify(fragments):
    if any(f.startswith("/etc/") or f.startswith("/usr/") for f in fragments):
        return "root"
    if any("/home/claude" in f for f in fragments):
        return "claude"
    return "unknown"


def _wd_ops(tree):
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


def _wd_command_strings(node):
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


class TestWatchdogSudoEscalation:
    """telegram_watchdog.py escalates every root write / root command and uses
    _sudo_user=claude for the /home/claude script. AST guard for the 2026-05-31
    deploy blocker."""

    def _tree(self):
        import ast

        path = ACCESS_TASKS_DIR / "telegram_watchdog.py"
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_etc_writes_have_sudo_no_sudo_user(self):
        tree = self._tree()
        env = _wd_assign_env(tree)
        found_root = 0
        for qual, node in _wd_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _wd_str_fragments(kw.value, env)
            if _wd_classify(frags) == "root":
                found_root += 1
                assert _wd_has_kw(node, "_sudo"), (
                    f"telegram_watchdog: {qual} writing root path {sorted(frags)} "
                    f"is MISSING _sudo=True (deploy connects AS claude)."
                )
                assert not _wd_has_kw(node, "_sudo_user"), (
                    f"telegram_watchdog: {qual} writing root path {sorted(frags)} "
                    f"must NOT set _sudo_user (escalate to root, not claude)."
                )
        # sudoers + timer + service = three /etc writes expected.
        assert found_root >= 3, (
            f"Expected >=3 root-owned /etc writes in telegram_watchdog.py "
            f"(sudoers + timer + service), classifier found {found_root}."
        )

    def test_home_claude_writes_have_sudo_user_claude(self):
        tree = self._tree()
        env = _wd_assign_env(tree)
        found_claude = 0
        for qual, node in _wd_ops(tree):
            if not qual.startswith("files."):
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    frags |= _wd_str_fragments(kw.value, env)
            if _wd_classify(frags) == "claude":
                found_claude += 1
                assert _wd_has_kw(node, "_sudo"), (
                    f"telegram_watchdog: {qual} writing claude path "
                    f"{sorted(frags)} is MISSING _sudo=True."
                )
                assert _wd_kw_is_claude(node, "_sudo_user"), (
                    f"telegram_watchdog: {qual} writing claude path "
                    f"{sorted(frags)} must set _sudo_user=\"claude\"."
                )
        # /home/claude/scripts/ dir + the .sh script = two claude writes.
        assert found_claude >= 2, (
            f"Expected >=2 claude-owned /home/claude writes (scripts dir + "
            f".sh), classifier found {found_claude}."
        )

    def test_systemctl_shell_commands_have_sudo(self):
        tree = self._tree()
        for qual, node in _wd_ops(tree):
            if qual != "server.shell":
                continue
            joined = "\n".join(_wd_command_strings(node))
            if "systemctl " in joined or "journalctl " in joined:
                assert _wd_has_kw(node, "_sudo"), (
                    f"telegram_watchdog: server.shell running {joined!r} is "
                    f"MISSING _sudo=True — systemctl is root-only."
                )

    def test_systemd_service_op_has_sudo(self):
        tree = self._tree()
        seen = 0
        for qual, node in _wd_ops(tree):
            if qual != "systemd.service":
                continue
            seen += 1
            assert _wd_has_kw(node, "_sudo"), (
                "telegram_watchdog: systemd.service (enable/start the timer) "
                "is MISSING _sudo=True — root-only operation."
            )
        assert seen >= 1, (
            "Expected a systemd.service op (enable+start the timer) in "
            "telegram_watchdog.py."
        )
