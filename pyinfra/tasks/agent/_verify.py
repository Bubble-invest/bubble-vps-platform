"""The Step 4 verification gate (SPEC-007 §"Migration path").

Runs AFTER the systemd unit is dropped + started, BEFORE _cleanup_legacy
deletes the plaintext fallback. If ANY check here fails, pyinfra aborts the
deploy — and the legacy plaintext path stays intact for a roll-forward fix
or rollback.

Six checks (all must pass):
    1. systemd service `claude-agent-<persona>.service` is `active`
    2. systemd service is `enabled` (auto-start at boot)
    3. /run/claude-agent/env exists and is readable by the claude user
       (we check via the systemd service's runtime — the file is mode 0400,
       owned root after decrypt then chowned to claude per the unit's
       ExecStartPre chain).
    4. The systemd unit DECLARES EnvironmentFile=/run/claude-agent/env
       (a static check on the rendered unit — protects against unit-template
       regressions).
    5. The systemd unit DECLARES the env vars CLAUDE_CODE_OAUTH_TOKEN and
       TELEGRAM_BOT_TOKEN are exposed (i.e. the EnvironmentFile substitution
       works — we verify by checking `systemctl show --property=Environment`
       contains the var NAMES, NEVER values).
    6. journalctl -u <service> --since="30 seconds ago" --priority=err
       returns zero error lines.

What we DELIBERATELY do NOT verify:
    - The actual values of CLAUDE_CODE_OAUTH_TOKEN / TELEGRAM_BOT_TOKEN
      (SPEC-008 hard rule: never echo plaintext credential to stdout).
    - End-to-end Telegram connectivity (deferred to manual smoke test by
      the parent agent — sending a Telegram DM to the bot expects a reply).

The goal is "we have very high confidence the new path works without
echoing a single credential byte to operator stdout."
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.operations import server

from lib.host_helpers import (
    agent_service_name,
    get_tenant_config,
    runtime_env_file,
)


def apply() -> None:
    """Run the six-check verification gate PER concierge (SPEC-001 v1.2).

    Each concierge has its own claude-agent-<name>.service + its own decrypted
    runtime env file; we gate each independently. If ANY concierge's checks
    fail, pyinfra aborts the deploy before _cleanup_legacy runs (the legacy
    plaintext rollback path stays intact for the whole box).
    """
    cfg = get_tenant_config(host)
    s = cfg.secrets
    if s is None or not s.enabled:
        return
    for i, concierge in enumerate(cfg.agent.concierges):
        # Each concierge names which blob key holds ITS Telegram bot token via
        # channels.telegram.bot_token_secret_ref (morty → TELEGRAM_BOT_TOKEN;
        # claudette → CLAUDETTE_TELEGRAM_BOT_TOKEN). None when telegram disabled.
        # The systemd unit REMAPS a non-primary ref onto TELEGRAM_BOT_TOKEN in
        # that concierge's env file, so the verify gate must check the remapped
        # name — _verify_one handles the swap given this ref.
        tg = concierge.channels.telegram
        bot_token_secret_ref = tg.bot_token_secret_ref if tg is not None else None
        _verify_one(
            s,
            concierge.name,
            is_primary=(i == 0),
            bot_token_secret_ref=bot_token_secret_ref,
        )


def _expected_env_var_names(s, bot_token_secret_ref) -> list[str]:
    """The env-var NAMES expected in a concierge's runtime env file.

    This accounts for the per-concierge bot-token REMAP that the systemd unit
    performs (`claude-agent.service.j2`). For a NON-PRIMARY concierge the unit
    DROPS the literal `TELEGRAM_BOT_TOKEN=` line and RENAMES the concierge's own
    `<ref>=` line → `TELEGRAM_BOT_TOKEN=`. So after remap the env file contains
    `TELEGRAM_BOT_TOKEN` (the remapped result) and NOT the original ref name.

    Derivation: start from `s.required_keys`; if the concierge's
    `bot_token_secret_ref` is set AND is NOT already `TELEGRAM_BOT_TOKEN`
    (i.e. a non-primary concierge whose token gets remapped), REPLACE that ref
    name with `TELEGRAM_BOT_TOKEN` and de-duplicate (preserving order). For the
    primary (ref == TELEGRAM_BOT_TOKEN, e.g. morty) the set is `required_keys`
    unchanged — morty already exposes TELEGRAM_BOT_TOKEN directly, no remap.

    Examples (required_keys = [CLAUDE_CODE_OAUTH_TOKEN, TELEGRAM_BOT_TOKEN,
    CLAUDETTE_TELEGRAM_BOT_TOKEN, TAILSCALE_AUTHKEY, PHONEHOME_TOKEN]):
      morty (ref=TELEGRAM_BOT_TOKEN)        → list unchanged
      claudette (ref=CLAUDETTE_TELEGRAM_…)  → CLAUDETTE_TELEGRAM_BOT_TOKEN swapped
                                              for TELEGRAM_BOT_TOKEN, then deduped
                                              (TELEGRAM_BOT_TOKEN already present).
    """
    remap = bot_token_secret_ref is not None and bot_token_secret_ref != "TELEGRAM_BOT_TOKEN"
    expected: list[str] = []
    for key in s.required_keys:
        # Swap the concierge's own ref name for its post-remap result.
        name = "TELEGRAM_BOT_TOKEN" if (remap and key == bot_token_secret_ref) else key
        if name not in expected:  # de-dup (TELEGRAM_BOT_TOKEN may now appear twice)
            expected.append(name)
    return expected


def _verify_one(
    s, persona_name: str, *, is_primary: bool, bot_token_secret_ref=None
) -> None:
    service = agent_service_name(persona_name)
    runtime_env = runtime_env_file(
        persona_name, is_primary=is_primary, primary_runtime_path=s.decrypted_runtime_path
    )
    expected_env_var_names = _expected_env_var_names(s, bot_token_secret_ref)

    # 1) Service active. `is-active` exits 0 if active, non-zero otherwise.
    #    pyinfra reports the operation Success on exit-0; if non-zero, the
    #    deploy aborts — which is exactly the gate we want before cleanup.
    # systemctl/journalctl/stat-on-0400-file all need root here. The deploy
    # connects AS claude; these system-unit + root-owned-runtime-file probes
    # require escalation. ROOT-owned operations → `_sudo=True` (no _sudo_user).
    # (The module docstring already assumed a global --sudo; we make it explicit
    # per-op so the gate works regardless of the deploy's global sudo flag.)
    server.shell(
        name=f"agent/verify: {service} is active",
        commands=[f"systemctl is-active --quiet {service}"],
        _sudo=True,
    )

    # 2) Service enabled (boot-survivable).
    server.shell(
        name=f"agent/verify: {service} is enabled",
        commands=[f"systemctl is-enabled --quiet {service}"],
        _sudo=True,
    )

    # 3) /run/<service>/env exists with mode 0400 and is owned by claude.
    #    `stat -c %a` prints "400". `stat -c %U` prints "claude". Two probes
    #    in one server.shell so we don't rack up four ops.
    server.shell(
        name=f"agent/verify: {runtime_env} present, mode 0400, owned by claude",
        commands=[
            f"test -f {runtime_env}",
            f"[ \"$(stat -c %a {runtime_env})\" = \"400\" ]",
            f"[ \"$(stat -c %U {runtime_env})\" = \"claude\" ]",
        ],
        # The runtime env file is mode 0400 (owner-read only). It lives under a
        # root-owned /run dir; stat/test from the connecting claude user can hit
        # Permission denied on the parent path. Escalate. ROOT op → `_sudo=True`.
        _sudo=True,
    )

    # 4) Static check on the unit: it declares EnvironmentFile=<runtime_env>.
    #    `systemctl show <service> --property=EnvironmentFiles` returns lines
    #    like `EnvironmentFiles=/run/claude-agent/env (ignore_errors=no)`.
    #    grep -q for the path substring. NO plaintext values touched.
    server.shell(
        name=f"agent/verify: unit declares EnvironmentFile={runtime_env}",
        commands=[
            f"systemctl show {service} --property=EnvironmentFiles | "
            f"grep -q '{runtime_env}'"
        ],
        # systemctl show on a system unit → escalate. ROOT op → `_sudo=True`.
        _sudo=True,
    )

    # 5) Verify the env-var NAMES are present in the on-disk EnvironmentFile.
    #    `systemctl show --property=Environment` would be the natural choice,
    #    but it ONLY reflects `Environment=` directives in the unit — values
    #    sourced from `EnvironmentFile=` are loaded at child-process spawn
    #    and never appear in the systemd-tracked Environment= property
    #    (verified empirically on Ubuntu 24.04 + systemd 255).
    #
    #    Alternative chosen here: `grep -q '^KEY=' /run/claude-agent/env`.
    #    The runtime env file is the on-disk artifact systemd will inject
    #    into the child process — checking key NAMES there proves the
    #    decryption produced the expected layout. The file is mode 0400
    #    root-then-claude; we read it via sudo (this op runs as root via
    #    the deploy's --sudo flag).
    #
    #    grep -q exits 0 on first match, 1 on no match; produces no stdout.
    #    Only KEY NAMES (anchored at line start with `^`) are matched —
    #    values never reach pyinfra's stdout. SPEC-008 hard rule honored.
    #
    #    We iterate the PER-CONCIERGE expected set (not the raw tenant
    #    required_keys): for a non-primary concierge the unit remaps her bot-token
    #    ref onto TELEGRAM_BOT_TOKEN and DROPS the original ref name, so checking
    #    the raw ref would false-fail. See _expected_env_var_names.
    for var_name in expected_env_var_names:
        server.shell(
            name=f"agent/verify: env var {var_name} present in {runtime_env}",
            commands=[
                f"grep -q '^{var_name}=' {runtime_env}",
            ],
            # 0400 root-owned-dir runtime env file → escalate. `_sudo=True`.
            _sudo=True,
        )

    # 6) journalctl error scan. `--priority=err` filters to err/crit/alert/emerg.
    #    `--since="30 seconds ago"` bounds the window so we only catch issues
    #    from this deploy (not unrelated past errors). `--no-pager -q` keeps
    #    output minimal. We assert ZERO lines via `wc -l` == 0 — done with
    #    a shell test that exits non-zero on any error.
    #
    #    No credential material in journal at err level (the plugin logs
    #    "TELEGRAM_BOT_TOKEN required" only on missing-token, which is the
    #    failure-mode we're guarding against — so even if it WERE there
    #    it'd be a token-name string, not the value).
    server.shell(
        name=f"agent/verify: no journal errors for {service} in last 30s",
        commands=[
            f"[ \"$(journalctl -u {service} "
            f"--since='30 seconds ago' --priority=err "
            f"--no-pager -q | wc -l)\" = \"0\" ]"
        ],
        # journalctl reading a system unit's journal requires root (or the
        # systemd-journal group, which claude is NOT in). ROOT op → `_sudo=True`.
        _sudo=True,
    )
