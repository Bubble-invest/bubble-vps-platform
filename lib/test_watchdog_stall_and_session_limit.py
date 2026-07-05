"""Render-assertion tests for the STALL (Signal 4b) and SESSION-LIMIT (Signal 7)
detectors added to the canonical VPS watchdog after the 2026-07-02 Geraldine (M5)
and Claudette outages (bubble-ops-board #485).

Style mirrors test_telegram_watchdog.py's Signal-5/6 tests: render the bash
template and assert the detection logic + recovery/alert branching is wired in.
This module is NOT data-repo-gated (it only renders the template with literal
kwargs), so it runs in CI and on any dev box — the end-to-end decision-logic
proof for the identical logic lives in the macOS twin's
tests/test_watchdog_decision_logic.py (runs the rendered bash with mocked
pending/transcript inputs and asserts which branch fires).

Run: python3.12 -m pytest lib/test_watchdog_stall_and_session_limit.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyinfra" / "templates"

_KW = {
    "unit_basename": "telegram-watchdog-morty",
    "persona_name": "morty",
    "service_name": "claude-agent-morty.service",
    "bot_pid_file": "/home/claude/.claude/channels/telegram/bot.pid",
    "decrypted_runtime_path": "/run/claude-agent/env",
    "cooldown_seconds": 300,
    "operator_telegram_user_id": "100000001",
    "last_restart_mark": "/run/telegram-watchdog-morty/last-restart",
    "session_projects_dir": "/home/claude/.claude/projects/-home-claude-agents-morty",
}


def _render(**over) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    kw = dict(_KW)
    kw.update(over)
    return env.get_template("telegram-watchdog.sh.j2").render(**kw)


# ─── The template must render at all ─────────────────────────────────────────

def test_template_renders_with_pyinfra_kwargs():
    """Regression guard: the watchdog template must render with EXACTLY the
    kwargs telegram_watchdog.py passes to files.template — no undefined vars.
    (A `{{OPERATOR}}` literal snuck into a comment during a hostname scrub and
    made the whole template un-renderable / deploy-blocking; this catches that
    class of bug at CI time.)"""
    rendered = _render()
    assert rendered.strip(), "template rendered empty"
    assert "{{" not in rendered and "}}" not in rendered, (
        "rendered watchdog still contains an un-substituted Jinja expression "
        "({{...}}) — a stray placeholder would break the real pyinfra deploy."
    )


# ─── Signal 4b: STALL (low-but-stuck poller) ─────────────────────────────────

def test_stall_detector_present_and_thresholded():
    """The stall detector must exist, key off pending being UNCHANGED across N
    ticks, and set broken=1 with a 'stall' reason so the existing recovery runs."""
    r = _render()
    assert "STALL_TICKS" in r, "Signal 4b missing: no STALL_TICKS threshold."
    assert "STALL_STATE" in r, "Signal 4b missing: no per-dept stall state file."
    assert re.search(r'broken=1\s*;?\s*\n?\s*reason="stall', r), (
        "Signal 4b must set broken=1 with a 'stall...' reason so the clean "
        "stop->start recovery path fires."
    )
    # Default of 3 ticks when the optional var isn't overridden.
    assert re.search(r"STALL_TICKS=3\b", r), (
        "STALL_TICKS should default to 3 (~15min at the 5-min cadence)."
    )


def test_stall_state_is_runtime_tmpfs():
    """The stall state file must live under /run (tmpfs, per-boot) like the other
    watchdog state — resets cleanly on reboot, never accumulates."""
    r = _render()
    m = re.search(r'STALL_STATE=("?)(/[^"\s]+)', r)
    assert m, "STALL_STATE assignment not found."
    assert m.group(2).startswith("/run/"), (
        f"STALL_STATE is at {m.group(2)!r} — must be under /run (tmpfs)."
    )


def test_stall_requires_nonzero_and_unchanged_and_no_activity():
    """Guard the false-positive design: the trip condition must require pending>0,
    pending==prev_pending (unchanged), AND no new transcript (tx==prev_tx). A
    single stuck tick or a fluctuating count must NOT trip."""
    r = _render()
    # The advance condition compares pending to the previous value AND the
    # transcript mtime to the previous mtime.
    assert re.search(r"pending == prev_p", r), (
        "Stall advance must require pending UNCHANGED vs last tick (== prev_p)."
    )
    assert re.search(r"tx_s4b == prev_tx", r), (
        "Stall advance must require NO new transcript activity (tx == prev_tx)."
    )
    # The trip requires pending>0 AND the sustained count reaching the threshold.
    assert re.search(r"pending > 0.*stall_count >= STALL_TICKS", r, re.DOTALL), (
        "Stall trip must require pending>0 AND stall_count>=STALL_TICKS."
    )
    # pending==0 must clear the counter (nothing stuck).
    assert re.search(r"pending == 0.*stall_count=0", r), (
        "pending=0 must reset the stall counter to 0."
    )


# ─── Signal 7: SESSION-LIMIT ─────────────────────────────────────────────────

def test_session_limit_detector_present():
    """The session-limit detector must parse the SDK fingerprint and branch on
    whether the reset time has passed."""
    r = _render()
    assert "session_limited" in r, "Signal 7 missing: no session-limit probe."
    assert "hit your session limit" in r, (
        "Signal 7 must match the SDK fingerprint 'hit your session limit'."
    )
    assert "resets" in r, "Signal 7 must parse the 'resets <clock>' reset time."


def test_session_limit_passed_restarts_future_alerts_only():
    """reset PASSED → broken=1 (restart via recovery); reset FUTURE → alert +
    exit WITHOUT restart. Both branches must be present and distinct."""
    r = _render()
    # passed → sets broken with a session-limit reason (falls into recovery).
    assert re.search(r'sl_reset_passed -eq 1.*broken=1', r, re.DOTALL), (
        "reset-PASSED branch must set broken=1 so the restart recovery runs."
    )
    assert re.search(r'reason="session-limit', r), (
        "The reset-passed restart reason must name session-limit."
    )
    # future → alert + exit 0, and must NOT set broken (never restarts).
    future_branch = r[r.find("sl_reset_passed -eq 0"):]
    assert future_branch, "reset-FUTURE branch not found."
    assert "exit 0" in future_branch, (
        "reset-FUTURE branch must exit WITHOUT restarting."
    )


def test_session_limit_future_branch_cannot_reach_restart():
    """Anti-restart-loop invariant: the reset-FUTURE branch must EXIT before the
    recovery block (systemctl stop). Structurally it can never restart-loop."""
    r = _render()
    # Locate the future-branch exit and the recovery stop.
    future_pos = r.find("reset ${sl_reset_raw} still in future")
    stop_pos = r.find('systemctl stop "$SERVICE"')
    assert future_pos != -1, "future-reset alert log line not found."
    assert stop_pos != -1, "recovery stop not found."
    assert future_pos < stop_pos, (
        "future-reset branch must appear BEFORE the recovery stop and exit there."
    )
    # Between the future alert and the stop there must be an `exit 0`.
    between = r[future_pos:stop_pos]
    assert "exit 0" in between, (
        "future-reset branch must `exit 0` before reaching the restart path — "
        "otherwise it could restart-loop against an unexpired limit."
    )


def test_session_limit_probe_runs_even_when_not_broken():
    """The whole point of Signal 7: the session-limit message is itself an
    assistant turn, so Signals 1-6 read 'healthy' (broken=0). The probe must
    therefore run BEFORE the `broken==0 -> ok/exit` gate, not be gated behind
    broken. Assert the probe appears before that gate."""
    r = _render()
    probe_pos = r.find("session_limited=0")
    ok_gate = r.find('if [[ $broken -eq 0 ]]; then\n    log "ok"')
    assert probe_pos != -1, "session-limit probe init not found."
    assert ok_gate != -1, "the broken==0 ok/exit gate not found."
    assert probe_pos < ok_gate, (
        "Signal 7 must be evaluated BEFORE the 'broken==0 -> ok' gate — the "
        "session-limit turn masquerades as healthy, so a probe gated behind "
        "broken would never run (the Claudette bug)."
    )


# ─── F1 regression: midnight-wrap reset classifier (deterministic) ───────────
#
# The VPS classifier is byte-identical to the Mac one. We extract its Python
# heredoc from the rendered script and drive it with an injected `now`, proving
# the midnight-wrap fix independent of when the suite runs.

import subprocess as _sp
import textwrap as _tw
from datetime import datetime as _dtmod, timezone as _tz


def _extract_reset_classifier(rendered: str) -> str:
    start = rendered.index("sl_reset_passed=$(python3 - \"$sl_reset_raw\" <<'PY'")
    body_start = rendered.index("\n", start) + 1
    body_end = rendered.index("\nPY\n", body_start)
    return rendered[body_start:body_end]


def _classify(reset_clock: str, now_utc) -> int:
    body = _extract_reset_classifier(_render())
    harness = _tw.dedent(f"""\
        import sys, datetime as _dt
        class _FrozenDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return _dt.datetime({now_utc.year},{now_utc.month},{now_utc.day},
                                    {now_utc.hour},{now_utc.minute},tzinfo=_dt.timezone.utc)
        _dt.datetime = _FrozenDT
        sys.argv = ["x", {reset_clock!r}]
    """) + body
    out = _sp.run(["python3", "-c", harness], capture_output=True, text=True, timeout=15)
    return int((out.stdout or "0").strip() or "0")


def test_f1_near_midnight_future_reset_is_alert_only():
    """F1 BLOCKER (reviewer counterexample): now=23:55, reset=12:10am is +15min
    FUTURE (tomorrow 00:10). Old same-day classifier read it as ~23h PAST →
    restart while the limit is active. Nearest-occurrence must classify FUTURE."""
    now = _dtmod(2026, 7, 2, 23, 55, tzinfo=_tz.utc)
    assert _classify("12:10am", now) == 0, (
        "near-midnight future reset must be alert-only (0), not restart (1) — F1."
    )


def test_f1_classifier_key_cases():
    UTC = _tz.utc
    assert _classify("5:10pm", _dtmod(2026, 7, 2, 17, 55, tzinfo=UTC)) == 1  # passed 45m
    assert _classify("5:10pm", _dtmod(2026, 7, 2, 16, 30, tzinfo=UTC)) == 0  # future 40m
    assert _classify("12:10am", _dtmod(2026, 7, 3, 0, 20, tzinfo=UTC)) == 1  # passed 10m
    assert _classify("5:10pm", _dtmod(2026, 7, 2, 17, 10, tzinfo=UTC)) == 0  # exact→conservative
    assert _classify("11:55pm", _dtmod(2026, 7, 3, 0, 5, tzinfo=UTC)) == 1   # passed 10m (yest)
