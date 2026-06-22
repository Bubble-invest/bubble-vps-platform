"""Install the daily cloud security audit cron (SPEC-014, Task B).

What this does, idempotently:
    1. Ensures /home/claude/scripts/ exists (mode 0755 owner claude:claude).
    2. Ensures /var/log/bubble-security/ exists (mode 0750 owner root:adm) —
       this is the canonical audit log directory; per-day log files inside
       are mode 0640 owner root:adm so they're readable by sudo + adm group
       only, NOT by the claude user (a compromised agent should NOT be able
       to read its own audit history).
    3. Renders /home/claude/scripts/security-audit.sh from a jinja2 template
       (mode 0755 owner claude:claude). The bash script implements the 8
       SPEC-014 audit parts (auth & access, secrets layer, agent behavior,
       package CVEs, disk & memory, transcript leak scan, claude version
       drift, hetzner cloud firewall) and posts a single summary message to
       Telegram via direct curl.
    4. Drops a sudoers rule at /etc/sudoers.d/claude-security-audit
       (mode 0440 owner root:root) — TIGHTLY scoped to the read commands
       the audit needs (fail2ban-client status, sshd -T, last, cat
       sudoers.d, cat /etc/passwd, journalctl) plus install/tee/grep for
       writing the audit log file as root:adm and running the leak scan.
       General sudo is NOT granted.
    5. Drops the systemd .timer + .service units at
       /etc/systemd/system/security-audit.{timer,service} (mode 0644
       owner root:root).
    6. systemctl daemon-reload, gated on either the timer or the service
       unit actually changing on disk.
    7. systemctl enable --now security-audit.timer.

Sudo escalation (deploy connects AS claude, NOT root): each op escalates
explicitly per the implementation-log.md rule. ROOT targets (/etc/sudoers.d,
/etc/systemd/system units, and the /var/log/bubble-security log dir — claude
can't mkdir under root-owned /var/log) + root-only systemctl commands →
`_sudo=True` ALONE (pyinfra applies the user=/group= ownership). CLAUDE targets
(/home/claude/scripts/{,security-audit.sh}) → `_sudo=True, _sudo_user="claude"`.
There is NO global pyinfra-as-root; missing escalation → Permission denied →
`No hosts remaining!` at deploy time.

SPEC-008 hard rule (no plaintext credential to stdout/stderr):
    The bash script reads TELEGRAM_BOT_TOKEN from /run/claude-agent/env into
    a shell variable and uses it briefly in HTTPS curl URLs to api.telegram.org,
    then immediately `unset TOKEN`. The token is never echoed, never logged.
    The transcript-leak scan (Part 6) uses `grep -lI` (filenames only, skip
    binaries) — NOT bare `grep`, which would echo the matched line containing
    the credential VALUE we're hunting for. See test_security_audit.py for
    static enforcement of both invariants.

EXPECTED_BOX_PUBKEY:
    The audit's Part 2 verifies /etc/age/key.pub on the box matches the
    public key stored in `bubble-vps-data/tenants/<tenant>/box-pubkey.txt`
    (the artifact emitted by tasks/secrets/_age_setup.py at first deploy).
    A drift means the box's age key was regenerated — a security event.
    The expected value is baked into the rendered script at deploy time.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server, systemd

from lib.host_helpers import get_tenant_config


_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_AUDIT_SH_TEMPLATE = _TEMPLATES_DIR / "security-audit.sh.j2"
_TIMER_TEMPLATE = _TEMPLATES_DIR / "security-audit.timer.j2"
_SERVICE_TEMPLATE = _TEMPLATES_DIR / "security-audit.service.j2"
_SUDOERS_TEMPLATE = _TEMPLATES_DIR / "sudoers-security-audit.j2"

_SCRIPT_DIR = "/home/claude/scripts"
_SCRIPT_PATH = "/home/claude/scripts/security-audit.sh"
_TIMER_PATH = "/etc/systemd/system/security-audit.timer"
_SERVICE_PATH = "/etc/systemd/system/security-audit.service"
_SUDOERS_PATH = "/etc/sudoers.d/claude-security-audit"

_AUDIT_LOG_DIR = "/var/log/bubble-security"


def _resolve_box_pubkey(host_) -> str:
    """Read the box's age public key from the data repo.

    Same path-resolution pattern as tasks/secrets/_age_setup.py:
    `host_.data.persona_dir` is the operator-Mac-side absolute path to the
    persona dir under tenants/<tenant>/persona/<persona-name>; the tenant
    directory is its grandparent; `box-pubkey.txt` lives there.

    Returns the trimmed pubkey string, or "" if the file doesn't exist
    (audit will then skip the drift check rather than always-FAIL).
    """
    persona_dir = Path(host_.data.persona_dir)
    pubkey_path = persona_dir.parent.parent / "box-pubkey.txt"
    if not pubkey_path.is_file():
        return ""
    return pubkey_path.read_text(encoding="utf-8").strip()


def apply() -> None:
    """Install the security audit timer + service + sudoers + script."""
    cfg = get_tenant_config(host)

    # The audit needs the agent's tenant context: persona name (→ service
    # name), the decrypted runtime env path (→ where to read the bot token),
    # and the operator's Telegram user id (→ where to post the summary).
    persona_name = cfg.agent.persona.name
    service_name = f"claude-agent-{persona_name}.service"

    s = cfg.secrets
    if s is None or not s.enabled:
        # Without the secrets layer there's no decrypted env file to read
        # the bot token from. The audit could still run its 8 checks, but
        # couldn't post to Telegram. Skip cleanly — same opt-out shape as
        # the watchdog.
        return
    decrypted_runtime_path = s.decrypted_runtime_path

    operator_telegram_user_id = cfg.contact.primary_telegram_user_id
    if not operator_telegram_user_id:
        # Per SPEC-014 reporting contract, the summary needs a chat_id to
        # send to. Without contact.primary_telegram_user_id, bail rather
        # than ship a half-broken audit.
        return

    expected_box_pubkey = _resolve_box_pubkey(host)

    # ─── 1. Ensure /home/claude/scripts/ exists ────────────────────────────
    files.directory(
        name="access/security_audit: ensure /home/claude/scripts/ exists",
        path=_SCRIPT_DIR,
        present=True,
        mode="0755",
        user="claude",
        group="claude",
        # CLAUDE target: deploy connects AS claude; escalate as claude so the
        # dir is owned claude:claude (not root). See implementation-log.md.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 2. Ensure /var/log/bubble-security/ exists (root:adm 0750) ────────
    # 0750 owner root, group adm: root + adm group can read; claude can NOT.
    # Per-day log files inside (audit-<date>.log) are mode 0640 owner root:adm
    # — same access pattern. A compromised agent should not be able to read
    # its own audit history.
    files.directory(
        name=f"access/security_audit: ensure {_AUDIT_LOG_DIR} exists (root:adm 0750)",
        path=_AUDIT_LOG_DIR,
        present=True,
        mode="0750",
        user="root",
        group="adm",
        # ROOT target: /var/log is root-owned — claude can't mkdir there.
        # Escalate to root ALONE (NO _sudo_user); pyinfra applies the
        # user="root"/group="adm" ownership. The dir is root:adm 0750 by
        # design so a compromised claude agent can't read its audit history.
        _sudo=True,
    )

    # ─── 3. Render the audit bash script ───────────────────────────────────
    files.template(
        name=f"access/security_audit: render {_SCRIPT_PATH}",
        src=str(_AUDIT_SH_TEMPLATE),
        dest=_SCRIPT_PATH,
        mode="0755",
        user="claude",
        group="claude",
        # Template variables (jinja2):
        service_name=service_name,
        decrypted_runtime_path=decrypted_runtime_path,
        operator_telegram_user_id=operator_telegram_user_id,
        audit_log_dir=_AUDIT_LOG_DIR,
        expected_box_pubkey=expected_box_pubkey,
        # CLAUDE target: script lives under /home/claude, owned claude:claude.
        _sudo=True,
        _sudo_user="claude",
    )

    # ─── 4. Drop sudoers rule (NOPASSWD scoped reads + log writers) ────────
    # Mode 0440 root:root — sudoers refuses to read files with looser perms.
    files.template(
        name=f"access/security_audit: drop sudoers at {_SUDOERS_PATH}",
        src=str(_SUDOERS_TEMPLATE),
        dest=_SUDOERS_PATH,
        mode="0440",
        user="root",
        group="root",
        # ROOT target: /etc/sudoers.d — root-only write (NO _sudo_user).
        _sudo=True,
    )

    # ─── 5. Drop systemd timer + service units ─────────────────────────────
    timer_op = files.template(
        name=f"access/security_audit: drop {_TIMER_PATH}",
        src=str(_TIMER_TEMPLATE),
        dest=_TIMER_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT target: /etc/systemd/system — root-only write (NO _sudo_user).
        _sudo=True,
    )
    service_op = files.template(
        name=f"access/security_audit: drop {_SERVICE_PATH}",
        src=str(_SERVICE_TEMPLATE),
        dest=_SERVICE_PATH,
        mode="0644",
        user="root",
        group="root",
        # ROOT target: /etc/systemd/system — root-only write (NO _sudo_user).
        _sudo=True,
    )

    # ─── 6. systemctl daemon-reload (gated) ────────────────────────────────
    # Re-reload only if EITHER the timer OR the service unit changed on disk.
    # Same lambda-wrapping gotcha as the watchdog: bound did_change methods
    # are always truthy, so we wrap in a lambda that's evaluated at exec time.
    server.shell(
        name="access/security_audit: systemctl daemon-reload (only if units changed)",
        commands=["systemctl daemon-reload"],
        _if=lambda: timer_op.did_change() or service_op.did_change(),
        # ROOT command: systemctl is root-only (NO _sudo_user).
        _sudo=True,
    )

    # ─── 7. Enable + start the timer ───────────────────────────────────────
    systemd.service(
        name="access/security_audit: enable + start security-audit.timer",
        service="security-audit.timer",
        enabled=True,
        running=True,
        # ROOT command: systemctl enable/start is root-only (NO _sudo_user).
        _sudo=True,
    )
    server.shell(
        name="access/security_audit: restart timer (only if timer unit changed)",
        commands=["systemctl restart security-audit.timer"],
        _if=timer_op.did_change,
        # ROOT command: systemctl is root-only (NO _sudo_user).
        _sudo=True,
    )
