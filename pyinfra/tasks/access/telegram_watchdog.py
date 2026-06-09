"""Install the Telegram plugin recovery watchdog (SPEC-013).

MULTI-CONCIERGE (SPEC-001 v1.2): a box may host several concierges (morty +
claudette). apply() LOOPS over cfg.agent.concierges and installs ONE fully
independent watchdog stack per concierge, with PERSONA-SUFFIXED unit names so
they never collide on a shared box. This realizes the "multi-agent box"
follow-up that SPEC-021's Architecture note flagged. We chose MIGRATION-B
(uniform suffixing for ALL concierges, INCLUDING morty) over keeping morty
un-suffixed as a special case: a regular scheme (no "morty is special" branch)
is simpler and matches how the bubble-ops-loop department watchdogs and the
claudette interim hand-deploy already name their units. The cost is a one-time
cleanup of morty's OLD un-suffixed units on the live box (see deploy notes).

What this does, idempotently, PER concierge (unit_basename =
`telegram-watchdog-<name>`):
    1. Ensures /home/claude/scripts/ exists (once, box-level).
    2. Renders /home/claude/scripts/<unit_basename>.sh from a jinja2 template
       (mode 0755 owner claude:claude). The bash script reads the concierge's
       OWN bot.pid + checks the cgroup-scoped bun poller + queries Telegram
       getWebhookInfo, stop→starts the agent service if any signal trips,
       alerts via direct curl on recovery failure.
    3. Drops a sudoers rule at /etc/sudoers.d/claude-<unit_basename>
       (mode 0440 owner root:root) — TIGHTLY scoped to just the systemctl
       operations the watchdog needs (stop/start/restart/is-active on
       claude-agent-<persona>.service). General sudo is NOT granted.
    4. Drops the systemd .timer + .service units at
       /etc/systemd/system/<unit_basename>.{timer,service}
       (mode 0644 owner root:root).
    5. systemctl daemon-reload, gated on any unit/sudoers file actually
       changing on disk. Same `_if=op.did_change` pattern Step 4's
       _systemd_unit.py uses for the agent service.
    6. systemctl enable --now <unit_basename>.timer.

SPEC-008 hard rule (no plaintext credential to stdout/stderr):
    The bash script reads TELEGRAM_BOT_TOKEN from the concierge's OWN decrypted
    runtime env file (primary → /run/claude-agent/env, others →
    /run/claude-agent-<name>/env) into a shell variable and uses it in HTTPS
    curl URLs to api.telegram.org, then immediately `unset TOKEN`. The token is
    never echoed, never logged, never written to disk (other than the read of
    the existing tmpfs file). See test_telegram_watchdog.py for static
    enforcement.

SUDOERS GOTCHA:
    pyinfra's files.template uses files.put under the hood, which copies
    bytes to the destination atomically — but it does NOT run `visudo -c`
    to validate the sudoers syntax before swapping the file in. If a
    malformed template ever lands here, sudo will refuse to operate on
    the box (since sudoers.d is parsed at every sudo invocation), which
    breaks the agent unit's sudo-based ExecStartPre + this very watchdog's
    `sudo systemctl restart`. The static test in
    lib/test_telegram_watchdog.py renders the template and asserts the
    syntax is well-formed (single-line NOPASSWD: rule, no shell meta);
    keep that test as the guardrail. Manual `visudo -cf` post-deploy is
    the operator's escape hatch if the static check ever misses something.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import (
    agent_service_name,
    agent_session_projects_dir,
    get_tenant_config,
    runtime_env_file,
    telegram_bot_pid_file,
    watchdog_unit_basename,
)


_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_WATCHDOG_SH_TEMPLATE = _TEMPLATES_DIR / "telegram-watchdog.sh.j2"
_TIMER_TEMPLATE = _TEMPLATES_DIR / "telegram-watchdog.timer.j2"
_SERVICE_TEMPLATE = _TEMPLATES_DIR / "telegram-watchdog.service.j2"
_SUDOERS_TEMPLATE = _TEMPLATES_DIR / "sudoers-telegram-watchdog.j2"
# SPEC-021 FIX-6 (Codex P1 hardening): root-owned helper that writes the
# transient resume drop-in with FIXED content, called by the watchdog via a
# tightly-scoped sudoers verb. Shared across ALL agents (one binary), so it's
# deployed once per host, not per persona.
_RESUME_DROPIN_HELPER_TEMPLATE = _TEMPLATES_DIR / "bubble-watchdog-resume-dropin.sh.j2"
_RESUME_DROPIN_HELPER_PATH = "/usr/local/bin/bubble-watchdog-resume-dropin"

_SCRIPT_DIR = "/home/claude/scripts"

# NOTE: the bot.pid path is PER-PERSONA — it is no longer a module constant.
# It is derived inside apply() from the persona name via
# lib.host_helpers.telegram_bot_pid_file(), the single source of truth shared
# with _telegram_plugin's state-dir creation. The old hardcoded
# "/home/claude/.claude/channels/telegram/bot.pid" was morty's channel dir;
# pointing every other agent's watchdog at morty's bot.pid made the watchdog
# read the wrong agent's liveness marker (FIX-2 / SPEC-021 invariant #4).
#
# SPEC-001 v1.2 (multi-concierge): the watchdog UNIT names, script path,
# sudoers drop-in, and RuntimeDirectory are now PERSONA-SUFFIXED
# (telegram-watchdog-<name>.*) so multiple concierges on one box never collide
# on unit names (the SPEC-021 "multi-agent box" follow-up, migration-b: EVERY
# concierge is suffixed, including morty). The last-restart cooldown mark lives
# INSIDE the suffixed RuntimeDirectory, /run/telegram-watchdog-<name>/.
_COOLDOWN_SECONDS = 300


def apply() -> None:
    """Install ONE Telegram watchdog (timer+service+sudoers+script) PER concierge.

    A box may host multiple concierges (morty + claudette). Each concierge's
    Telegram plugin needs its own liveness watchdog, and on a shared box those
    watchdog units MUST be persona-suffixed (telegram-watchdog-<name>.*) so they
    never collide. We loop over cfg.agent.concierges and render a fully
    independent watchdog stack per concierge.
    """
    cfg = get_tenant_config(host)

    s = cfg.secrets
    if s is None or not s.enabled:
        # Without the secrets layer there's no decrypted env file to read
        # the bot token from. The watchdog would still detect process
        # liveness, but couldn't alert. Skip cleanly.
        return

    joris_telegram_user_id = cfg.contact.primary_telegram_user_id
    if not joris_telegram_user_id:
        # Per SPEC-013, the recovery-failure alert needs a chat_id to send
        # to. Without contact.primary_telegram_user_id, the "alert via direct
        # curl" path can't escalate. Bail rather than ship a half-broken
        # watchdog.
        return

    # /home/claude/scripts/ is shared across concierges — create it once. The
    # deploy connects AS claude but pyinfra still needs explicit escalation to
    # set ownership/mode reliably. This is a CLAUDE-OWNED target (under
    # /home/claude) → `_sudo=True, _sudo_user="claude"` (NOT bare root — the dir
    # must end up owned by claude).
    files.directory(
        name="access/telegram_watchdog: ensure /home/claude/scripts/ exists",
        path=_SCRIPT_DIR,
        present=True,
        mode="0755",
        user="claude",
        group="claude",
        _sudo=True,
        _sudo_user="claude",
    )

    for i, concierge in enumerate(cfg.agent.concierges):
        _apply_one(
            concierge.name,
            s,
            joris_telegram_user_id,
            is_primary=(i == 0),
        )


def _apply_one(persona_name, s, joris_telegram_user_id, *, is_primary: bool) -> None:
    """Render + lifecycle one concierge's persona-suffixed watchdog stack."""
    service_name = agent_service_name(persona_name)

    # Persona-suffixed unit basename — drives EVERY watchdog artifact for this
    # concierge so two concierges on one box can never collide.
    unit_basename = watchdog_unit_basename(persona_name)  # telegram-watchdog-<name>
    script_path = f"{_SCRIPT_DIR}/{unit_basename}.sh"
    timer_path = f"/etc/systemd/system/{unit_basename}.timer"
    service_path = f"/etc/systemd/system/{unit_basename}.service"
    sudoers_path = f"/etc/sudoers.d/claude-{unit_basename}"
    timer_unit = f"{unit_basename}.timer"
    # The cooldown mark lives inside this concierge's OWN RuntimeDirectory
    # (/run/telegram-watchdog-<name>/) so two concierges don't share cooldown.
    last_restart_mark = f"/run/{unit_basename}/last-restart"

    # Per-persona bot.pid path — the watchdog's PRIMARY health signal. Derived
    # from the SAME single-source helper the plugin-state-dir creation uses, so
    # the watchdog can never read the wrong agent's liveness marker on a
    # multi-agent box (FIX-2 / SPEC-021 invariant #4). morty → bare telegram/,
    # other concierges → telegram-<persona>/.
    bot_pid_file = telegram_bot_pid_file(persona_name)

    # The agent's session-transcript dir (FIX-4b / SPEC-021 inv#4d). claude
    # names its per-project jsonl dir by taking the absolute WorkingDirectory
    # (/home/claude/agents/<persona>, UNPREFIXED per inv#6) and replacing every
    # "/" with "-". The watchdog tails the newest jsonl there to detect a 401
    # auth failure that a restart cannot fix.
    session_projects_dir = agent_session_projects_dir(persona_name)

    # Per-concierge decrypted runtime env file — where THIS concierge's
    # TELEGRAM_BOT_TOKEN lives. Primary keeps the historical path; others use
    # /run/claude-agent-<name>/env (matches the agent service unit + claudette's
    # hand-deploy).
    decrypted_runtime_path = runtime_env_file(
        persona_name, is_primary=is_primary, primary_runtime_path=s.decrypted_runtime_path
    )

    # ─── 0. Root-owned resume-drop-in helper (shared, fixed-content) ───────
    # SPEC-021 FIX-6 hardening (Codex P1): the watchdog must NOT be granted raw
    # `sudo tee` against a systemd drop-in (arbitrary content → root escalation).
    # Instead it calls this root-owned helper, which writes FIXED content and
    # allowlists the service name. One binary for all agents; deploying it on
    # each persona's apply() is idempotent (files.template is content-stable).
    files.template(
        name=f"access/telegram_watchdog: render {_RESUME_DROPIN_HELPER_PATH}",
        src=str(_RESUME_DROPIN_HELPER_TEMPLATE),
        dest=_RESUME_DROPIN_HELPER_PATH,
        mode="0755",
        user="root",
        group="root",
        service_name=service_name,  # template ignores it; kept for render parity
        _sudo=True,
    )

    # ─── 1. Render the watchdog bash script (persona-suffixed path) ────────
    files.template(
        name=f"access/telegram_watchdog: render {script_path}",
        src=str(_WATCHDOG_SH_TEMPLATE),
        dest=script_path,
        mode="0755",
        user="claude",
        group="claude",
        # Template variables (jinja2):
        unit_basename=unit_basename,
        persona_name=persona_name,
        service_name=service_name,
        bot_pid_file=bot_pid_file,
        decrypted_runtime_path=decrypted_runtime_path,
        cooldown_seconds=_COOLDOWN_SECONDS,
        joris_telegram_user_id=joris_telegram_user_id,
        last_restart_mark=last_restart_mark,
        session_projects_dir=session_projects_dir,
        # CLAUDE-OWNED target (/home/claude/scripts/...) → escalate to claude so
        # the script ends up owned by claude:claude (NOT root). `_sudo=True,
        # _sudo_user="claude"`.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 2. Drop sudoers rule (NOPASSWD stop+start+restart+is-active) ──────
    # Mode 0440 root:root — sudoers refuses to read files with looser perms.
    # Persona-suffixed drop-in path so concierges don't clobber each other's
    # rule. See module docstring for the visudo-validation gotcha.
    files.template(
        name=f"access/telegram_watchdog: drop sudoers at {sudoers_path}",
        src=str(_SUDOERS_TEMPLATE),
        dest=sudoers_path,
        mode="0440",
        user="root",
        group="root",
        service_name=service_name,
        unit_basename=unit_basename,
        # ROOT-owned target (/etc/sudoers.d/...) → `_sudo=True` ALONE (no
        # _sudo_user; writing as claude would still be Permission denied on
        # /etc). The deploy connects AS claude, which cannot write /etc.
        _sudo=True,
    )

    # ─── 3. Drop systemd timer + service units (persona-suffixed) ──────────
    timer_op = files.template(
        name=f"access/telegram_watchdog: drop {timer_path}",
        src=str(_TIMER_TEMPLATE),
        dest=timer_path,
        mode="0644",
        user="root",
        group="root",
        unit_basename=unit_basename,
        persona_name=persona_name,
        # ROOT-owned target (/etc/systemd/system/...) → `_sudo=True` ALONE.
        _sudo=True,
    )
    service_op = files.template(
        name=f"access/telegram_watchdog: drop {service_path}",
        src=str(_SERVICE_TEMPLATE),
        dest=service_path,
        mode="0644",
        user="root",
        group="root",
        unit_basename=unit_basename,
        persona_name=persona_name,
        # ROOT-owned target (/etc/systemd/system/...) → `_sudo=True` ALONE.
        _sudo=True,
    )

    # ─── 4. systemctl daemon-reload (gated) ────────────────────────────────
    # Re-reload only if EITHER the timer OR the service unit changed on disk.
    # Sudoers is excluded — systemd doesn't reload sudoers, and the sudoers
    # change has no effect on whether the unit needs re-loading.
    #
    # GOTCHA: pyinfra's `did_change` is a bound METHOD on the operation result;
    # it must be called by pyinfra (it's evaluated at execution time, not at
    # definition time). Doing `bool(timer_op.did_change or service_op.did_change)`
    # in Python evaluates two bound-methods which are always truthy → daemon-reload
    # always fires, breaking idempotency. Instead, wrap in a lambda that is
    # called by pyinfra at the right moment and checks BOTH at that point.
    server.shell(
        name=f"access/telegram_watchdog: systemctl daemon-reload (only if {unit_basename} units changed)",
        commands=["systemctl daemon-reload"],
        _if=lambda: timer_op.did_change() or service_op.did_change(),
        # systemctl daemon-reload on system units is root-only → `_sudo=True`.
        _sudo=True,
    )

    # ─── 5. Enable + start the timer ───────────────────────────────────────
    # systemd.service is idempotent: it checks is-enabled / is-active before
    # issuing enable/start. On a unit-content change we ALSO restart the
    # timer (so OnBootSec/OnUnitActiveSec re-derive cleanly), gated on
    # timer_op.did_change.
    # systemctl enable/start on a system timer is root-only → `_sudo=True`.
    systemd.service(
        name=f"access/telegram_watchdog: enable + start {timer_unit}",
        service=timer_unit,
        enabled=True,
        running=True,
        _sudo=True,
    )
    # systemctl restart on a system timer is root-only → `_sudo=True`.
    server.shell(
        name=f"access/telegram_watchdog: restart {timer_unit} (only if timer unit changed)",
        commands=[f"systemctl restart {timer_unit}"],
        _if=timer_op.did_change,
        _sudo=True,
    )
