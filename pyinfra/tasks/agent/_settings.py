"""Template ~/.claude/settings.json + seed ~/.claude.json trust state (SPEC-009).

Per SPEC-009 §"Updated settings.json template", settings.json contains NO
secrets and NO env-var references. It's a purely declarative config: which
plugin enabled, default permissions mode, PATH for the bun runtime.

Why we ALSO touch ~/.claude.json:
    The first time claude runs in an unfamiliar working directory it shows
    a TTY-blocking "Quick safety check: Is this a project you created..."
    workspace-trust dialog. The supervised systemd service has no operator
    to click "Yes" — and the reasonable answer for a single-tenant VPS
    where the entire box is the agent's sandbox is "trust the working
    directory". We pre-populate the trust acknowledgment by writing
    `projects.<workdir>.hasTrustDialogAccepted = true` into ~/.claude.json
    (the file claude itself uses to persist this state after a manual
    "Yes" click — same shape, same path).

    The merge is shallow: we only set the `hasTrustDialogAccepted` field;
    every other key (numStartups, tipsHistory, lastSessionId, etc.) stays
    untouched. Idempotent: re-runs only mutate the file if the trust value
    needs flipping.

Idempotency:
    files.template hashes both sides; only re-writes when bytes differ.
    The ~/.claude.json patcher uses a Python helper that exits without
    writing if the trust state is already correct.
"""

from __future__ import annotations

import json
from pathlib import Path

from pyinfra import host
from pyinfra.facts.server import Command
from pyinfra.operations import files, server

from lib.host_helpers import as_claude, get_tenant_config


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "claude-settings.json.j2"
)
_REMOTE_PATH = "/home/claude/.claude/settings.json"
_CLAUDE_USER_JSON = "/home/claude/.claude.json"

# Canonical model id for ALL departments (SPEC-021 invariant #1).
# `opus[1m]` = the auto-upgrading "opus" family alias + the `[1m]` 1M-context
# modifier. We DELIBERATELY do NOT pin a version (e.g. `claude-opus-4-8[1m]`):
# the whole point is auto-upgrade. The bare `opus` alias resolves to the LATEST
# Opus release, so when 4.9+ ships our headless agents pick it up automatically
# instead of being stranded on an old model. The `[1m]` modifier keeps the
# 1M-context window. Verified on the live box (morty's working ExecStart uses
# this exact alias): `opus[1m]` resolves to "Claude Opus 4.8 (1M context)" today
# and will track newer Opus releases. The literal "default" was the actual
# broken value (the 2026-05-31 outage root cause). Any tenant that does not pin
# its own agent.llm.model inherits this.
CANONICAL_MODEL = "opus[1m]"


def _trust_seed_command(workdir: str) -> str:
    """Build a single-line shell command that idempotently sets
    `projects.<workdir>.hasTrustDialogAccepted=true` in ~/.claude.json.

    We could write the python script to a tempfile and exec it, but that's
    extra round-trips. Instead we base64-encode the script and pipe it to
    `python3 -` via `su - claude`. This sidesteps every shell quoting layer
    (sh single-quote, bash double-quote, ssh outer quote) cleanly.

    Strategy embedded in the script:
        1. Read ~/.claude.json (or {} if missing).
        2. Set d['projects'][workdir]['hasTrustDialogAccepted'] = True.
        3. Write back via tempfile + rename (atomic, mode 0600).

    Idempotency:
        Caller (apply()) runs a fact-based pre-check that gates this op
        out entirely if the value is already correct. So this command is
        only invoked when a mutation is actually needed.
    """
    import base64
    py = (
        "import json,os,sys,tempfile\n"
        f"P={_CLAUDE_USER_JSON!r}\n"
        f"W={workdir!r}\n"
        "try:\n"
        "  with open(P) as f: d=json.load(f)\n"
        "except FileNotFoundError:\n"
        "  d={}\n"
        "d.setdefault('projects',{})\n"
        "p=d['projects'].setdefault(W,{})\n"
        "if p.get('hasTrustDialogAccepted') is True: sys.exit(0)\n"
        "p['hasTrustDialogAccepted']=True\n"
        "fd,tmp=tempfile.mkstemp(dir=os.path.dirname(P),prefix='.claude.json.')\n"
        "with os.fdopen(fd,'w') as f: json.dump(d,f)\n"
        "os.chmod(tmp,0o600)\n"
        "os.rename(tmp,P)\n"
    )
    encoded = base64.b64encode(py.encode("utf-8")).decode("ascii")
    # Run AS claude WITHOUT a password. pyinfra connects as claude already, so
    # bare `su - claude` would self-su-prompt for a password and abort the
    # deploy. The base64 | base64 -d | python3 - pipeline must run wholly in
    # the claude shell, so wrap it in `sh -c '...'` before as_claude —
    # otherwise `sudo -n -u claude` (root fallback) would only run `echo` as
    # claude. The encoded blob has no shell-special chars (base64 alphabet is
    # [A-Za-z0-9+/=]), so it survives the `sh -c '...'` single-quote layer
    # intact.
    return as_claude(f"sh -c 'echo {encoded} | base64 -d | python3 -'")


def apply() -> None:
    """Render settings.json (once, box-level) + seed trust state PER concierge.

    settings.json lives at ~/.claude/settings.json — it is per-USER (box-level),
    NOT per-concierge, and every concierge uses the same canonical `opus[1m]`
    model + acceptEdits mode, so we render it once from the PRIMARY concierge's
    config. The workspace-trust acknowledgment in ~/.claude.json, however, is
    keyed by WORKDIR — each concierge has its own UNPREFIXED workdir
    /home/claude/agents/<name> (SPEC-021 inv#6) and needs its own trust seed,
    so that loops per concierge. The per-concierge settings-changed restart also
    loops (each concierge has its own claude-agent-<name>.service).
    """
    cfg = get_tenant_config(host)
    primary = cfg.agent.concierges[0]

    # Ensure ~/.claude/ exists with claude:claude ownership. files.directory
    # is idempotent.
    # CLAUDE-OWNED target → `_sudo=True, _sudo_user="claude"` (mirror of
    # _persona.py / _telegram_plugin.py claude-owned writes). The deploy
    # connects AS claude; escalating to the same user lets pyinfra enforce
    # ownership/mode reliably.
    files.directory(
        name="agent/settings: ensure /home/claude/.claude exists",
        path="/home/claude/.claude",
        user="claude",
        group="claude",
        mode="0755",
        present=True,
        _sudo=True,
        _sudo_user="claude",
    )

    # Permission mode + model:
    #   - permission_mode: defaults to "acceptEdits" — auto-approves file edits +
    #     safe filesystem commands, prompts for non-trivial Bash. Was "auto" but
    #     reverted 2026-05-09: auto mode shows a TTY-bound opt-in prompt that
    #     systemd can't dismiss (claude blocks waiting for keypress that never
    #     comes, plugin never spawns, agent appears active but is dead). Until
    #     we figure out the headless auto-mode opt-in (Phase 5d follow-up),
    #     stay on acceptEdits.
    #   - model: defaults to the auto-upgrading alias `opus[1m]` (the "opus"
    #     family alias, which resolves to the LATEST Opus, + the `[1m]`
    #     1M-context modifier). We intentionally do NOT pin a version: {{OPERATOR}}
    #     wants auto-upgrade so agents move to Opus 4.9+ automatically when it
    #     ships instead of being stranded on an old pinned id. The literal
    #     "default" was the broken value (the 2026-05-31 outage root cause):
    #     claude errored "There's an issue with the selected model (default).
    #     It may not exist" and the session never started (the Telegram plugin
    #     never spawned, the agent looked active to systemd but was dead).
    #     `opus[1m]` resolves deterministically (verified live — morty's
    #     working ExecStart uses it), so we hardcode it as the fallback
    #     DEFAULT. A tenant.yaml MAY still override via agent.llm.model, but
    #     the default must be a model alias that actually resolves. The
    #     empty-string guard below ALSO maps any falsy/empty tenant value to
    #     the canonical alias so the rendered settings.json can never contain
    #     `"model": "default"` by accident. See SPEC-021.
    # Both can be overridden per-tenant via tenant.yaml's agent.permission_mode
    # / agent.model fields (when those land in SPEC-001 v2).
    permission_mode = getattr(cfg.agent, "permission_mode", None) or "acceptEdits"
    # CANONICAL_MODEL: the one pinned model id every department must default to.
    # Kept as a module-level constant so SPEC-021's invariant has a single
    # source of truth (the test asserts the rendered settings.json defaults to
    # exactly this value). We render from the PRIMARY concierge's model — all
    # concierges on a box share the same canonical alias, and settings.json is
    # per-USER not per-concierge.
    model = primary.llm.model if primary.llm.model else CANONICAL_MODEL

    # CLAUDE-OWNED target (/home/claude/.claude/settings.json) →
    # `_sudo=True, _sudo_user="claude"`.
    settings_op = files.template(
        name="agent/settings: write /home/claude/.claude/settings.json",
        src=str(_TEMPLATE_PATH),
        dest=_REMOTE_PATH,
        user="claude",
        group="claude",
        mode="0644",
        permission_mode=permission_mode,
        model=model,
        _sudo=True,
        _sudo_user="claude",
    )

    # Per concierge: trust-seed its workdir + restart its service on a
    # settings.json change. Both are keyed by the concierge's UNPREFIXED workdir
    # / service name (SPEC-021 inv#6 + multi-concierge).
    for concierge in cfg.agent.concierges:
        _seed_and_restart(concierge.name, settings_op)


def _seed_and_restart(persona_name: str, settings_op) -> None:
    """Restart one concierge's service if settings.json changed + seed its
    workspace-trust acknowledgment for its UNPREFIXED workdir."""
    workdir = f"/home/claude/agents/{persona_name}"

    # Settings.json changes are NOT picked up by a running claude process —
    # only at restart. Wire the same restart-on-change pattern as the systemd
    # unit task: if the file actually changed, kick a restart. Service name
    # mirrors _systemd_unit.py's convention.
    #
    # Edge case: on a COLD deploy, _settings.apply() runs BEFORE _systemd_unit
    # has created the service. The shell command tolerates this with a
    # systemctl-list-units guard — only restart if the service actually
    # exists. On a warm deploy (service already running), the restart fires
    # and the new settings take effect within a few seconds.
    service_name = f"claude-agent-{persona_name}.service"
    server.shell(
        name=f"agent/settings: restart {service_name} if settings.json changed",
        commands=[
            f"systemctl list-unit-files {service_name} >/dev/null 2>&1 && "
            f"systemctl restart {service_name} || "
            f"echo 'service not yet installed — first-deploy path; _systemd_unit will start it'"
        ],
        _if=settings_op.did_change,
        _sudo=True,
    )

    # Seed the workspace-trust acknowledgment for the agent's working dir.
    # Pre-check fact: a 1-line grep against the current ~/.claude.json. If
    # we already see this workdir flagged as trusted (substring match — an
    # exact JSON path-walk would need quoting gymnastics), skip the mutation.
    # This keeps Step 4's idempotency acceptance criterion (zero-mutation
    # re-runs) clean.
    #
    # The substring check is "weak" but safe: false positives would mean
    # we don't re-write the file when something subtly changed, which is
    # acceptable; false negatives just trigger an idempotent rewrite.
    # Pre-check: read ~/.claude.json and verify projects[workdir]
    # .hasTrustDialogAccepted == True. We base64-encode the python script
    # (same trick as the seed command) to avoid any sh/bash quoting layer.
    import base64 as _b64
    _check_py = (
        "import json,sys\n"
        f"P={_CLAUDE_USER_JSON!r}\n"
        f"W={workdir!r}\n"
        "try:\n"
        "  with open(P) as f: d=json.load(f)\n"
        "except Exception:\n"
        "  print('notyet'); sys.exit(0)\n"
        "p=d.get('projects',{}).get(W,{})\n"
        "print('accepted' if p.get('hasTrustDialogAccepted') is True else 'notyet')\n"
    )
    _check_b64 = _b64.b64encode(_check_py.encode("utf-8")).decode("ascii")
    pre_check = host.get_fact(
        Command,
        command=f"echo {_check_b64} | base64 -d | python3 -",
    )
    if pre_check is None or pre_check.strip() != "accepted":
        server.shell(
            name=f"agent/settings: seed workspace-trust acknowledgment for {workdir}",
            commands=[_trust_seed_command(workdir)],
        )
