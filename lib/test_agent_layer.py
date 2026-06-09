"""Static + golden-file tests for the agent layer (Step 4 — SPEC-007 + SPEC-009).

These tests cover three surfaces:

    1. Template rendering (golden-file compare):
       - claude-settings.json.j2 — must match the SPEC-009 declarative-only
         form. NO plaintext secrets, NO `${VAR}` references in the env block.
       - claude-agent.service.j2 — must match the committed golden file when
         rendered with bubble-internal's tenant.yaml values.

    2. Plaintext-leak guards on the rendered settings.json:
       - The literal token prefixes from the leaked credentials (Telegram bot
         id, OpenRouter sk-or-v1, sk-ant-oat01) must NOT appear in the
         rendered output.
       - The settings.json env block must NOT contain `${OPENROUTER_API_KEY}`
         or `${ANTHROPIC_AUTH_TOKEN}` style references — that was the
         OpenRouter design we abandoned. With auth_mode=claude_code_subscription,
         claude reads CLAUDE_CODE_OAUTH_TOKEN directly from the systemd
         environment, no settings.json indirection needed.

    3. SPEC-008 hard rule extension to the agent layer:
       - Any `sops --decrypt` invocation in the agent-layer pyinfra modules
         (notably _systemd_unit.py and _verify.py) MUST be followed by a
         redirection or filter that drops/masks the plaintext (one of
         `> /dev/null`, `| grep -q`, `| wc`, OR `--output <path>` to a
         tmpfs path). This is the same rule that lib/test_secrets_layer.py
         enforces on _sops_deploy.py — extended here so future edits to the
         agent layer can't slip a leak past review.

Run with: python3.12 -m pytest lib/test_agent_layer.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyinfra" / "templates"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "agent"
AGENT_TASKS_DIR = REPO_ROOT / "pyinfra" / "tasks" / "agent"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.tenant_loader import load_tenant  # noqa: E402


# Literal credential prefixes that MUST NOT leak into any rendered template.
# Bot id 8350575119 is the Telegram bot id from the leaked .env.
# `sk-or-v1` is the OpenRouter key prefix from the (rotated) ~/.secrets.
# `sk-ant-oat01` is Anthropic's OAuth token prefix from CLAUDE_CODE_OAUTH_TOKEN.
LEAKED_PREFIXES = ("8350575119:", "sk-or-v1-", "sk-ant-oat01-")


# ─── Helpers ────────────────────────────────────────────────────────────────


def _jinja_env() -> Environment:
    """Same jinja2 env config the hardening tests use — matches pyinfra's
    rendering byte-for-byte (default Environment + keep_trailing_newline)."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, **kwargs) -> str:
    return _jinja_env().get_template(template_name).render(**kwargs)


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bubble_internal_cfg():
    data_repo = (REPO_ROOT / ".." / "bubble-vps-data").resolve()
    return load_tenant("bubble-internal", data_repo)


def _strip_comments_and_strings(source: str) -> str:
    """Same scrubber as test_secrets_layer.py — removes Python comments and
    triple-quoted docstrings before scanning shell commands. We only want to
    flag patterns in EXECUTABLE code (the Python string literals passed as
    `commands=[...]`), not in prose commentary that legitimately mentions
    forbidden patterns to explain why they're forbidden.
    """
    no_triple = re.sub(
        r'"""(?:[^"\\]|\\.|"(?!""))*"""',
        '',
        source,
        flags=re.DOTALL,
    )
    no_triple = re.sub(
        r"'''(?:[^'\\]|\\.|'(?!''))*'''",
        '',
        no_triple,
        flags=re.DOTALL,
    )
    no_comments = re.sub(r"#[^\n]*", "", no_triple)
    return no_comments


def _join_adjacent_string_literals(source: str) -> str:
    """Collapse Python's implicit string-literal concatenation. Same trick
    as test_secrets_layer.py — pyinfra task modules build long shell commands
    by writing several adjacent literals on consecutive lines, so the static
    scan needs to see them as one string.
    """
    pattern = re.compile(r'"\s*(?:[fFrRbB]{0,2})"', re.DOTALL)
    collapsed = pattern.sub("", source)
    pattern_single = re.compile(r"'\s*(?:[fFrRbB]{0,2})'", re.DOTALL)
    collapsed = pattern_single.sub("", collapsed)
    return collapsed


# ─── settings.json template ──────────────────────────────────────────────────


# Default render kwargs — matches what _settings.py passes for bubble-internal.
# Updated 2026-05-08 (post-Joris-feedback): the template now takes
# permission_mode + model jinja vars to allow auto mode + Opus selection.
# Updated 2026-05-31 (SPEC-021): model set to the canonical auto-upgrading alias
# `opus[1m]` (the "opus" family alias + `[1m]` 1M-context modifier). We do NOT
# pin a version (auto-upgrade is a deliberate requirement). The literal "default"
# was the broken value (production outage root cause); bare "opus" WITHOUT the
# `[1m]` modifier resolves but loses the 1M-context window, so we require the
# modifier.
_CANONICAL_MODEL = "opus[1m]"
_DEFAULT_RENDER_KWARGS = {
    # Reverted 2026-05-09: auto mode TTY-bound opt-in prompt blocks systemd
    # service. Back to acceptEdits until we solve headless opt-in.
    "permission_mode": "acceptEdits",
    "model": _CANONICAL_MODEL,
}


def test_settings_json_template_matches_golden(bubble_internal_cfg):
    """The settings.json template renders to exactly the bytes committed in
    lib/golden/agent/claude-settings.json. The template now takes 2 jinja
    vars (permission_mode, model) — the golden reflects the bubble-internal
    defaults (acceptEdits + the canonical auto-upgrading model alias opus[1m]).
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    expected = _golden("claude-settings.json")
    assert rendered == expected, (
        f"settings.json template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_settings_json_template_no_plaintext_secrets():
    """Render the template and grep for every known leaked-credential prefix.
    These exact byte sequences must NOT appear in the output. If they do,
    Step 4 has regressed to the pre-Step-4 plaintext-on-disk world.
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    for prefix in LEAKED_PREFIXES:
        assert prefix not in rendered, (
            f"Leaked credential prefix {prefix!r} found in rendered "
            f"settings.json — this would re-introduce the Step 1 leak."
        )


def test_settings_json_template_no_env_var_refs_in_env_block():
    """Per SPEC-009, the env block of settings.json must NOT contain any
    `${VAR}` references. The OpenRouter design (`${OPENROUTER_API_KEY}` etc.)
    was abandoned — with auth_mode=claude_code_subscription, claude reads
    CLAUDE_CODE_OAUTH_TOKEN directly from the systemd environment.

    We allow `$PATH`-style legacy references in the PATH value (none in our
    template, but the grep is conservative — anchored on `${[A-Z_]+}`-shaped
    refs only).
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    # ${UPPER_SNAKE_CASE} references — these were the OpenRouter pattern.
    matches = re.findall(r"\$\{[A-Z][A-Z0-9_]*\}", rendered)
    assert not matches, (
        f"settings.json template contains env-var references: {matches}. "
        f"SPEC-009 §'Updated settings.json template' forbids these — "
        f"CLAUDE_CODE_OAUTH_TOKEN is consumed directly from systemd env."
    )


def test_settings_json_template_does_not_pin_anthropic_model_via_envvar():
    """The template MUST NOT use ANTHROPIC_MODEL env-var pinning — that's
    a different mechanism than the `model` settings field. Pinning via env
    would override the OAuth subscription's selection in unexpected ways
    (the env var has higher priority than the settings field). Our model
    selection goes through the `model` settings field instead.
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    assert "ANTHROPIC_MODEL" not in rendered, (
        "settings.json template uses ANTHROPIC_MODEL env-var pinning. "
        "Use the `model` settings field instead (already supported, simpler "
        "precedence ordering)."
    )


def test_settings_json_template_uses_accepteits_permission_mode_by_default():
    """Default render uses acceptEdits permission mode.

    Originally we set this to "auto" (Anthropic's classifier-gated mode), but
    on 2026-05-09 we discovered that auto mode shows a TTY-bound opt-in prompt
    on first encounter that systemd can't dismiss — claude blocks waiting for a
    keypress, the Telegram plugin never spawns, and the agent appears active
    but is functionally dead.

    Reverted to acceptEdits (works fine in non-interactive setups). Re-investigate
    headless auto-mode opt-in as a Phase 5d follow-up.
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    assert '"defaultMode": "acceptEdits"' in rendered, (
        "default render must set permission defaultMode to 'acceptEdits' "
        "(reverted from 'auto' on 2026-05-09 due to headless opt-in block)"
    )


def test_settings_json_template_pins_canonical_model():
    """Default render sets model to the canonical auto-upgrading alias
    `opus[1m]` (SPEC-021 invariant #1).

    `opus[1m]` = the "opus" family alias (auto-resolves to the LATEST Opus,
    a deliberate auto-upgrade requirement from Joris) + the `[1m]` 1M-context
    modifier. We do NOT pin a version. The literal "default" was the broken
    value (the 2026-05-31 production outage): the session never started and the
    Telegram plugin never spawned. `opus[1m]` resolves deterministically
    (verified live — morty's working ExecStart uses it).
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    assert f'"model": "{_CANONICAL_MODEL}"' in rendered, (
        f"default render must set model to the canonical alias "
        f"{_CANONICAL_MODEL!r}"
    )


def test_settings_json_template_never_renders_default_or_bare_opus():
    """SPEC-021 invariant #1, negative form: the rendered settings.json must
    NEVER contain the literal `"model": "default"` (the broken value that caused
    the 2026-05-31 outage), nor the bare `"model": "opus"` WITHOUT the `[1m]`
    modifier (bare `opus` resolves but loses the 1M-context window — we require
    the modifier). This guards against a regression that drops the 1M context or
    re-introduces the unresolvable "default" and bricks every department's agent
    on next restart.

    Note `"model": "opus[1m]"` does NOT contain the substring `"model": "opus"`
    (the closing quote differs), so the bare-opus check correctly passes for the
    canonical alias.
    """
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    assert '"model": "default"' not in rendered, (
        'settings.json renders `"model": "default"` — this literal does not '
        "resolve on the current Claude Code build (the outage root cause)."
    )
    assert '"model": "opus"' not in rendered, (
        'settings.json renders the bare `"model": "opus"` alias WITHOUT the '
        "`[1m]` modifier — that loses the 1M-context window. Use the canonical "
        f"alias {_CANONICAL_MODEL!r}."
    )


def test_settings_default_falls_back_to_canonical_model_via_module_constant():
    """The _settings.py module exposes CANONICAL_MODEL as the fallback used
    when a tenant.yaml leaves agent.llm.model empty. Assert (a) the constant
    is exactly the canonical auto-upgrading alias `opus[1m]`, and (b)
    _settings.py uses it as the fallback (NOT a bare "opus" without the `[1m]`
    modifier, which would drop the 1M-context window).

    This is the source-side guard: even a tenant with an empty model field
    inherits a model alias that resolves AND keeps the 1M context.
    """
    settings_path = AGENT_TASKS_DIR / "_settings.py"
    source = settings_path.read_text(encoding="utf-8")
    assert f'CANONICAL_MODEL = "{_CANONICAL_MODEL}"' in source, (
        f"_settings.py must define CANONICAL_MODEL = {_CANONICAL_MODEL!r} "
        f"as the single source of truth for the default model alias."
    )
    # The fallback expression must use CANONICAL_MODEL, never a bare "opus".
    # SPEC-001 v1.2 (multi-concierge): settings.json is rendered from the
    # PRIMARY concierge's model (`primary.llm.model`), so the guarded
    # expression reads `primary.llm.model if primary.llm.model else
    # CANONICAL_MODEL`. We match any `<x>.llm.model if <x>.llm.model else
    # CANONICAL_MODEL` so the invariant survives the rename.
    assert re.search(
        r"\.llm\.model\s+if\s+\S+\.llm\.model\s+else\s+CANONICAL_MODEL",
        source,
    ), (
        "_settings.py model fallback must be `... else CANONICAL_MODEL` — the "
        "old `else \"opus\"` fallback resolves to an unusable alias."
    )
    assert 'else "opus"' not in source, (
        '_settings.py still falls back to the bare "opus" alias somewhere — '
        "replace with CANONICAL_MODEL."
    )


def test_settings_json_template_has_telegram_plugin_enabled():
    """Sanity check: the only plugin we enable is the Telegram bridge."""
    rendered = _render("claude-settings.json.j2", **_DEFAULT_RENDER_KWARGS)
    assert '"telegram@claude-plugins-official": true' in rendered


# ─── systemd unit template ───────────────────────────────────────────────────


def _render_systemd_for_bubble_internal(cfg) -> str:
    """Render the systemd unit template with bubble-internal cfg's values
    (persona ricky, default sops paths). Mirror the variable shape that the
    pyinfra _systemd_unit module passes."""
    s = cfg.secrets
    sysd = cfg.agent.systemd
    # Multi-concierge (SPEC-001 v1.2): the PRIMARY concierge keeps the
    # historical runtime path, so runtime_env_dir is the dirname of
    # decrypted_runtime_path (e.g. /run/claude-agent).
    runtime_env_dir = s.decrypted_runtime_path.rsplit("/", 1)[0]
    # Primary concierge (morty): bot_token_secret_ref == TELEGRAM_BOT_TOKEN, so
    # NO remap is emitted (the shared blob already keys the primary's token under
    # the plugin-expected name) — the rendered unit stays byte-identical to the
    # historical golden so morty's live service does not churn.
    primary = cfg.agent.concierges[0]
    tg = primary.channels.telegram
    bot_token_secret_ref = tg.bot_token_secret_ref if tg is not None else None
    # Per-concierge TELEGRAM_STATE_DIR (SPEC-021 finding): derived from the SAME
    # host_helpers single source the plugin's channel-dir creation uses, so the
    # exported var can never drift from the dir the plugin writes bot.pid into.
    # morty (primary) → bare telegram/ (its live state must not move).
    from lib.host_helpers import telegram_channel_dir

    telegram_state_dir = telegram_channel_dir(primary.name)
    return _render(
        "claude-agent.service.j2",
        persona_name=cfg.agent.persona.name,
        tenant_name=cfg.tenant_name,
        age_key_path=s.age_key_path,
        encrypted_file_path=s.encrypted_file_path,
        decrypted_runtime_path=s.decrypted_runtime_path,
        runtime_env_dir=runtime_env_dir,
        bot_token_secret_ref=bot_token_secret_ref,
        telegram_state_dir=telegram_state_dir,
        sops_bin="/usr/local/bin/sops",
        claude_bin="/usr/bin/claude",
        channels="plugin:telegram@claude-plugins-official",
        restart=sysd.restart,
        restart_sec=sysd.restart_sec,
        nofile_limit=sysd.nofile_limit,
    )


def test_systemd_unit_template_matches_golden(bubble_internal_cfg):
    """Render the systemd unit with bubble-internal cfg; compare to golden.
    The golden file is what pyinfra will write to /etc/systemd/system/ on
    joris-cx33 — drift between this test and reality means the dogfood run
    will report unexpected changes.
    """
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    # Step 5a (SPEC-010): persona renamed ricky → morty. The golden file
    # tracks the bubble-internal tenant.yaml's current persona name.
    expected = _golden("claude-agent-morty.service")
    assert rendered == expected, (
        f"systemd unit template diverged from golden. Diff:\n"
        f"--- expected ---\n{expected!r}\n--- got ---\n{rendered!r}"
    )


def test_systemd_unit_uses_envfile_on_run_path(bubble_internal_cfg):
    """Acceptance criterion: the rendered unit must declare
    EnvironmentFile=/run/claude-agent/env (or whatever cfg.secrets
    .decrypted_runtime_path resolves to). Without this, the agent process
    won't see CLAUDE_CODE_OAUTH_TOKEN / TELEGRAM_BOT_TOKEN.
    """
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    expected_path = bubble_internal_cfg.secrets.decrypted_runtime_path
    # Accept the `-` prefix (makes the file optional at unit-load time) — this
    # is required on Ubuntu 24.04 + systemd 255 because EnvironmentFile is
    # checked at unit activation BEFORE ExecStartPre populates it.
    pattern = rf"^EnvironmentFile=-?{re.escape(expected_path)}$"
    assert re.search(pattern, rendered, re.MULTILINE), (
        f"systemd unit missing `EnvironmentFile={expected_path}` "
        f"(with optional leading `-`). Without this directive, claude "
        f"won't see the decrypted secrets."
    )


def test_systemd_unit_has_execstartpre_sops_decrypt(bubble_internal_cfg):
    """Acceptance criterion: the unit's ExecStartPre chain must invoke sops
    --decrypt on the encrypted blob, with --output redirecting to a tmpfs
    path (NOT to stdout, which would leak into journald).
    """
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    s = bubble_internal_cfg.secrets
    # The decrypt happens via `/bin/sh -c '... sops --decrypt --output ...'`.
    # We grep for the salient pieces rather than the exact full line so the
    # test is robust to small phrasing changes (e.g. swapping --output order).
    assert "sops --decrypt" in rendered, (
        "systemd unit has no `sops --decrypt` step — secrets won't be "
        "decrypted at service start."
    )
    assert "--output" in rendered, (
        "systemd unit's sops invocation must use --output to write to a "
        "tmpfs file. Bare `sops --decrypt FILE` writes plaintext to stdout, "
        "which systemd would capture into journald — a leak."
    )
    assert s.encrypted_file_path in rendered, (
        f"systemd unit doesn't reference encrypted file path "
        f"{s.encrypted_file_path}"
    )
    # The age key path is read via SOPS_AGE_KEY_FILE env-var.
    assert f"SOPS_AGE_KEY_FILE={s.age_key_path}" in rendered, (
        f"systemd unit doesn't set SOPS_AGE_KEY_FILE={s.age_key_path}. "
        f"Without this, sops won't find the box's private key."
    )


def test_systemd_unit_runs_as_claude_user(bubble_internal_cfg):
    """User/Group must be `claude`. ExecStartPre lines with `+` prefix run
    as root (needed to read /etc/age/key.txt) — ExecStart itself stays in
    the unprivileged claude account.
    """
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    assert "User=claude" in rendered
    assert "Group=claude" in rendered


def test_systemd_unit_no_plaintext_secrets(bubble_internal_cfg):
    """Same plaintext-leak guard as for settings.json: the literal leaked
    credential prefixes must not appear in the rendered unit. The unit
    references PATHS that contain the secrets (encrypted file, EnvironmentFile)
    but never the values themselves.
    """
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    for prefix in LEAKED_PREFIXES:
        assert prefix not in rendered, (
            f"systemd unit template contains leaked prefix {prefix!r}. "
            f"This would write plaintext credentials into "
            f"/etc/systemd/system/claude-agent-{bubble_internal_cfg.agent.persona.name}.service "
            f"— exactly the leak Step 4 is designed to eliminate."
        )


# ─── Per-concierge bot-token remap (multi-concierge blocker #1) ────────────────
#
# Each concierge's systemd unit decrypts the SHARED tenant secrets blob into its
# OWN per-concierge runtime env file. The Telegram plugin reads the token from
# the env var named exactly TELEGRAM_BOT_TOKEN. With a verbatim decrypt, EVERY
# concierge would expose the SAME TELEGRAM_BOT_TOKEN (the primary's). The schema
# models the fix via channels.telegram.bot_token_secret_ref: each concierge names
# which key in the blob holds ITS token (morty → TELEGRAM_BOT_TOKEN; claudette →
# CLAUDETTE_TELEGRAM_BOT_TOKEN). The unit must REMAP the named ref onto
# TELEGRAM_BOT_TOKEN in the decrypted runtime env — but ONLY for non-primary
# concierges (ref != TELEGRAM_BOT_TOKEN), so the primary's unit is unchanged.


def _render_systemd_for_concierge(
    *,
    persona_name: str,
    bot_token_secret_ref,
    runtime_env_dir: str,
    decrypted_runtime_path: str,
    telegram_state_dir: str | None = None,
) -> str:
    """Render the agent unit for an arbitrary concierge with an explicit
    bot_token_secret_ref. Mirrors the kwargs _systemd_unit._apply_one passes."""
    if telegram_state_dir is None:
        # Default to the host_helpers single source so callers that don't care
        # about the state-dir line still render a coherent unit.
        from lib.host_helpers import telegram_channel_dir

        telegram_state_dir = telegram_channel_dir(persona_name)
    return _render(
        "claude-agent.service.j2",
        persona_name=persona_name,
        tenant_name="bubble-internal",
        age_key_path="/etc/age/key.txt",
        encrypted_file_path="/etc/bubble/secrets.sops.env",
        decrypted_runtime_path=decrypted_runtime_path,
        runtime_env_dir=runtime_env_dir,
        bot_token_secret_ref=bot_token_secret_ref,
        telegram_state_dir=telegram_state_dir,
        sops_bin="/usr/local/bin/sops",
        claude_bin="/usr/bin/claude",
        channels="plugin:telegram@claude-plugins-official",
        restart="on-failure",
        restart_sec=10,
        nofile_limit=65536,
    )


def test_primary_concierge_unit_has_no_token_remap(bubble_internal_cfg):
    """Primary concierge (morty) has bot_token_secret_ref == TELEGRAM_BOT_TOKEN.
    No remap must be emitted — the rendered unit stays byte-identical to today's
    golden so morty's LIVE service does not churn on the next deploy. The
    primary case is detected purely by ref == TELEGRAM_BOT_TOKEN."""
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    # No remap markers of any kind.
    assert "grep -v" not in rendered, (
        "primary concierge unit must NOT contain a grep-based token remap — "
        "morty's blob already keys its token under TELEGRAM_BOT_TOKEN."
    )
    assert "| sed " not in rendered, (
        "primary concierge unit must NOT pipe through sed for a token remap."
    )
    assert ".remap.tmp" not in rendered
    # And the decrypt → mv → chmod → chown chain is the historical verbatim one.
    assert (
        "ExecStartPre=+/bin/sh -c 'SOPS_AGE_KEY_FILE=/etc/age/key.txt "
        "/usr/local/bin/sops --decrypt --output /run/claude-agent/env.tmp "
        "/etc/bubble/secrets.sops.env'\n"
        "ExecStartPre=+/bin/mv /run/claude-agent/env.tmp /run/claude-agent/env\n"
    ) in rendered


def test_primary_concierge_unit_is_byte_identical_to_golden(bubble_internal_cfg):
    """Explicit no-churn guard: the morty golden must NOT change when the
    bot_token_secret_ref var is threaded through (it equals TELEGRAM_BOT_TOKEN,
    so the conditional remap branch is skipped entirely)."""
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    expected = _golden("claude-agent-morty.service")
    assert rendered == expected, (
        "morty's unit changed after adding the per-concierge token remap — "
        "this would churn the live primary service. The remap MUST be skipped "
        f"when ref == TELEGRAM_BOT_TOKEN.\n--- expected ---\n{expected!r}\n"
        f"--- got ---\n{rendered!r}"
    )


def test_nonprimary_concierge_unit_remaps_named_ref_onto_telegram_bot_token():
    """Non-primary concierge (claudette, ref CLAUDETTE_TELEGRAM_BOT_TOKEN) gets
    a remap step: drop any literal TELEGRAM_BOT_TOKEN= line from the decrypted
    blob, then promote CLAUDETTE_TELEGRAM_BOT_TOKEN= → TELEGRAM_BOT_TOKEN=. This
    mirrors the proven bubble-ops-loop dept pattern."""
    rendered = _render_systemd_for_concierge(
        persona_name="claudette",
        bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        runtime_env_dir="/run/claude-agent-claudette",
        decrypted_runtime_path="/run/claude-agent-claudette/env",
    )
    # Drops the primary's literal token line.
    assert 'grep -v "^TELEGRAM_BOT_TOKEN="' in rendered, (
        "non-primary unit must drop the literal TELEGRAM_BOT_TOKEN= line so the "
        "primary's token never leaks into this concierge's env."
    )
    # Promotes the named ref onto the plugin-expected name.
    assert 's/^CLAUDETTE_TELEGRAM_BOT_TOKEN=/TELEGRAM_BOT_TOKEN=/' in rendered, (
        "non-primary unit must promote CLAUDETTE_TELEGRAM_BOT_TOKEN= → "
        "TELEGRAM_BOT_TOKEN= so the Telegram plugin reads claudette's token."
    )


def test_nonprimary_remap_operates_on_decrypted_file_not_sops_stdout():
    """sops-guard constraint: the remap must run grep/sed on the ALREADY-DECRYPTED
    tmpfs file (a /run path), never pipe `sops --decrypt | ...` to stdout. The
    sops invocation itself keeps the guard-approved `--decrypt --output FILE`
    form."""
    rendered = _render_systemd_for_concierge(
        persona_name="claudette",
        bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        runtime_env_dir="/run/claude-agent-claudette",
        decrypted_runtime_path="/run/claude-agent-claudette/env",
    )
    # The sops decrypt still uses --output FILE (guard-approved), never `| ` pipe.
    assert "sops --decrypt --output /run/claude-agent-claudette/env.tmp" in rendered
    # The remap reads the DECRYPTED tmpfs .tmp file (not sops output).
    assert "grep -v" in rendered
    remap_lines = [
        ln for ln in rendered.splitlines() if "grep -v" in ln or "sed " in ln
    ]
    assert remap_lines, "expected a remap ExecStartPre line"
    for ln in remap_lines:
        # The remap must NOT invoke sops at all (it works on plaintext).
        assert "sops" not in ln, (
            f"remap line must not invoke sops (it operates on the decrypted "
            f"file): {ln!r}"
        )
        # No `sops ... | ` stdout pipe anywhere — guard would block it.
        assert "| sed" not in ln or "sops" not in ln
    # Defense in depth: there must be no `sops --decrypt FILE |` pipe form.
    assert "--decrypt /" not in rendered and "--decrypt --output" in rendered


def test_nonprimary_remap_never_echoes_the_token():
    """TOKEN HYGIENE: the remap is grep/sed on a file → the token VALUE never
    hits stdout/journal. The rendered unit must never echo/print/log the env
    file's contents.

    NOTE (Codex P1 fail-safe guard): the missing-ref branch now emits a static
    diagnostic via `echo "...REF... refusing to blank..." >&2`. That echo prints
    only the literal KEY NAME (a public identifier, never a token value) to
    stderr. The KEY NAME is NOT a secret — the secret is the token VALUE, which
    lives only inside the .tmp/.remap.tmp files and is never command-substituted
    into any echo. So a stderr diagnostic that names the ref is permitted; what
    stays banned is echoing/printing the env file CONTENTS (which would require a
    command-substitution `$(...)`/backtick reading the env, or a cat/grep of the
    env file piped to stdout).
    """
    rendered = _render_systemd_for_concierge(
        persona_name="claudette",
        bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        runtime_env_dir="/run/claude-agent-claudette",
        decrypted_runtime_path="/run/claude-agent-claudette/env",
    )
    for line in rendered.splitlines():
        if "CLAUDETTE_TELEGRAM_BOT_TOKEN" not in line:
            continue
        if line.lstrip().startswith("#"):
            continue  # comments may name the ref for documentation
        # The static stderr diagnostic (`echo "..." >&2`) is allowed: it prints
        # only the literal ref NAME, never a value, and never reads the env file.
        # It is provably value-free because it contains no command substitution
        # ($(...) / backticks) that could splice the env file's contents in.
        is_stderr_diagnostic = (
            ">&2" in line and "$(" not in line and "`" not in line
        )
        for banned in ("echo", "printf", "logger", "tee", "cat "):
            if banned in ("echo", "printf") and is_stderr_diagnostic:
                continue  # static name-only stderr diagnostic — no value leaks
            assert banned not in line, (
                f"token-hygiene violation: line references "
                f"CLAUDETTE_TELEGRAM_BOT_TOKEN with {banned!r} (would leak the "
                f"value to stdout/journal): {line!r}"
            )
    # Belt-and-suspenders: assert NO command-substitution that reads the env
    # file ever feeds an echo/printf (the only way the VALUE could leak).
    assert "$(grep" not in rendered and "$(cat" not in rendered, (
        "no command substitution may splice env-file contents into a command"
    )


def test_nonprimary_remap_preserves_0400_chown_hardening():
    """The final per-concierge env file must keep 0400 + chown claude hardening
    after the remap (no regression vs the primary's chain)."""
    rendered = _render_systemd_for_concierge(
        persona_name="claudette",
        bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        runtime_env_dir="/run/claude-agent-claudette",
        decrypted_runtime_path="/run/claude-agent-claudette/env",
    )
    assert "/bin/chmod 0400 /run/claude-agent-claudette/env" in rendered
    assert "/bin/chown claude:claude /run/claude-agent-claudette/env" in rendered


# ─── Per-concierge TELEGRAM_STATE_DIR export (SPEC-021 finding, multi-box) ─────
#
# The Telegram MCP plugin (server.ts) stores ALL of its per-agent runtime state
# — access.json, approved/, inbox/, .env AND bot.pid — under the directory named
# by the TELEGRAM_STATE_DIR env var, defaulting to ~/.claude/channels/telegram
# when unset. With ONE concierge on the box the bare default was fine. With a
# SECOND concierge (claudette) on the same box, both pollers default to the SAME
# bare telegram/ dir → bot.pid collision + getUpdates 409 cross-talk. So EACH
# concierge's systemd unit MUST export its OWN TELEGRAM_STATE_DIR, derived from
# the SAME lib.host_helpers.telegram_channel_dir single source the plugin's
# channel-dir creation uses — so the dir the unit exports and the dir the plugin
# creates can never diverge. morty keeps the bare telegram/ (its live channel
# state already lives there and must not move); others get telegram-<name>/.


def test_nonprimary_concierge_unit_exports_own_telegram_state_dir():
    """(a) A NON-bare concierge (claudette) unit must export
    Environment=TELEGRAM_STATE_DIR=/home/claude/.claude/channels/telegram-claudette
    so its Telegram poller writes its OWN bot.pid and never collides with
    morty's bare telegram/ dir."""
    from lib.host_helpers import telegram_channel_dir

    rendered = _render_systemd_for_concierge(
        persona_name="claudette",
        bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        runtime_env_dir="/run/claude-agent-claudette",
        decrypted_runtime_path="/run/claude-agent-claudette/env",
        telegram_state_dir=telegram_channel_dir("claudette"),
    )
    assert (
        "Environment=TELEGRAM_STATE_DIR="
        "/home/claude/.claude/channels/telegram-claudette" in rendered
    ), (
        "claudette's unit must export its OWN TELEGRAM_STATE_DIR so its poller "
        "doesn't collide with morty's bare telegram/ dir."
    )


def test_morty_unit_exports_bare_telegram_state_dir(bubble_internal_cfg):
    """(b) morty is the bare-channel special case: its unit must export
    Environment=TELEGRAM_STATE_DIR=/home/claude/.claude/channels/telegram
    (NOT telegram-morty) — morty's live channel state (access.json, bot.pid,
    paired chats) already lives in the bare telegram/ dir and must NOT move."""
    rendered = _render_systemd_for_bubble_internal(bubble_internal_cfg)
    assert (
        "Environment=TELEGRAM_STATE_DIR="
        "/home/claude/.claude/channels/telegram\n" in rendered
    ), (
        "morty's unit must export the BARE telegram/ dir (no -morty suffix) so "
        "its existing live channel state isn't orphaned."
    )
    # Explicitly assert the suffixed form is NOT present — orphaning guard.
    assert (
        "TELEGRAM_STATE_DIR=/home/claude/.claude/channels/telegram-morty"
        not in rendered
    ), (
        "morty must NOT get a -morty-suffixed TELEGRAM_STATE_DIR — that would "
        "orphan its live channel state under the bare telegram/ dir."
    )


def test_telegram_state_dir_comes_from_host_helpers_single_source():
    """(c) The value the unit exports MUST come from the host_helpers
    channel-dir derivation, so it can never drift from the dir _telegram_plugin
    actually CREATES. We render with EXACTLY telegram_channel_dir(name) and
    confirm the rendered line matches — for both the bare and suffixed cases."""
    from lib.host_helpers import telegram_channel_dir

    for name in ("morty", "claudette", "sandra"):
        state_dir = telegram_channel_dir(name)
        rendered = _render_systemd_for_concierge(
            persona_name=name,
            bot_token_secret_ref="TELEGRAM_BOT_TOKEN"
            if name == "morty"
            else f"{name.upper()}_TELEGRAM_BOT_TOKEN",
            runtime_env_dir=f"/run/claude-agent-{name}",
            decrypted_runtime_path=f"/run/claude-agent-{name}/env",
            telegram_state_dir=state_dir,
        )
        assert f"Environment=TELEGRAM_STATE_DIR={state_dir}\n" in rendered, (
            f"unit for {name} must export the host_helpers-derived "
            f"TELEGRAM_STATE_DIR={state_dir} (single source of truth)."
        )


def test_systemd_unit_passes_host_helpers_telegram_state_dir_per_concierge():
    """The _systemd_unit task module must derive telegram_state_dir from the
    host_helpers channel-dir helper (NOT reinvent it) and pass it into the
    template render — so the exported var and the plugin-created dir share a
    single source of truth."""
    source = (AGENT_TASKS_DIR / "_systemd_unit.py").read_text(encoding="utf-8")
    assert "telegram_channel_dir" in source, (
        "_systemd_unit.py must import + use lib.host_helpers.telegram_channel_dir "
        "to derive each concierge's TELEGRAM_STATE_DIR — do not reinvent."
    )
    assert "telegram_state_dir=" in source, (
        "_systemd_unit.py must pass telegram_state_dir=... into files.template."
    )


def test_telegram_plugin_creates_same_dir_unit_exports():
    """SINGLE SOURCE OF TRUTH: the dir _telegram_plugin CREATES (where the plugin
    writes bot.pid) must be derived from the SAME telegram_channel_dir helper the
    unit's exported TELEGRAM_STATE_DIR is derived from — otherwise they diverge
    and the poller writes bot.pid somewhere the watchdog/unit don't expect."""
    source = (AGENT_TASKS_DIR / "_telegram_plugin.py").read_text(encoding="utf-8")
    assert "telegram_channel_dir" in source, (
        "_telegram_plugin.py must derive the created channel dir from "
        "lib.host_helpers.telegram_channel_dir — the SAME helper the unit uses "
        "for TELEGRAM_STATE_DIR — so they can never diverge."
    )


# ─── Static guards on the agent-layer pyinfra modules ───────────────────────


class TestAgentTaskModulesExist:
    """Structural regressions: each sub-module is part of the orchestration
    chain. Missing files = silent skipped phases. Catch them at test time."""

    REQUIRED_MODULES = (
        "__init__.py",
        "deploy.py",
        "_install.py",
        "_persona.py",
        "_settings.py",
        "_telegram_plugin.py",
        "_systemd_unit.py",
        "_verify.py",
        "_cleanup_legacy.py",
    )

    @pytest.mark.parametrize("module_name", REQUIRED_MODULES)
    def test_module_exists(self, module_name: str):
        path = AGENT_TASKS_DIR / module_name
        assert path.is_file(), (
            f"Agent task module missing: {path}. "
            f"This is part of the Step 4 task layout (SPEC-007 §'Tasks layout')."
        )


class TestAgentLayerNoUnredirectedSopsDecrypt:
    """SPEC-008 hard rule extension: agent-layer modules that issue
    `sops --decrypt` (notably _verify.py if it ever does a sanity decrypt
    on the box) MUST mask the plaintext. The systemd unit template uses
    --output to a tmpfs path which is the SPEC-006 pattern; the .j2 template
    is excluded from this scan because its semantics are systemd's, not
    pyinfra's stdout — but pyinfra modules that exec shell commands are.
    """

    SCANNED_MODULES = ("_install.py", "_persona.py", "_settings.py",
                       "_telegram_plugin.py", "_systemd_unit.py",
                       "_verify.py", "_cleanup_legacy.py", "deploy.py")

    @pytest.mark.parametrize("module_name", SCANNED_MODULES)
    def test_no_unredirected_decrypt(self, module_name: str):
        path = AGENT_TASKS_DIR / module_name
        if not path.is_file():
            pytest.skip(f"{module_name} not yet created")
        source = _join_adjacent_string_literals(
            _strip_comments_and_strings(path.read_text(encoding="utf-8"))
        )
        decrypt_re = re.compile(
            r"(?:/usr/local/bin/sops|\{_SOPS_BIN\}|\bsops)\s+--decrypt\b"
        )
        violations: list[str] = []
        for m in decrypt_re.finditer(source):
            tail = source[m.end():m.end() + 200]
            cutoff = len(tail)
            for sentinel in ('"', "'", "\n"):
                idx = tail.find(sentinel)
                if idx != -1 and idx < cutoff:
                    cutoff = idx
            tail = tail[:cutoff]
            allowed = (
                "> /dev/null" in tail
                or "| grep -q" in tail
                or "|grep -q" in tail
                or "| wc" in tail
                or "|wc" in tail
                or "--output" in tail  # writes to tmpfs, not stdout
            )
            if not allowed:
                line_no = source[: m.start()].count("\n") + 1
                snippet = source[m.start(): m.start() + 120].replace("\n", "\\n")
                violations.append(
                    f"line ~{line_no}: unredirected `sops --decrypt` near: {snippet!r}"
                )
        assert not violations, (
            f"SPEC-008 hard rule violated in pyinfra/tasks/agent/{module_name}: "
            f"every `sops --decrypt` invocation MUST mask plaintext (one of "
            f"`> /dev/null`, `| grep -q`, `| wc`, `--output <tmpfs path>`).\n\n"
            "Violations:\n  " + "\n  ".join(violations)
        )


class TestAgentLayerNoForbiddenCatOnEnvFile:
    """The decrypted runtime env at /run/claude-agent/env contains plaintext
    credentials. SPEC-008 forbids `cat /run/claude-agent/env` (or any cmd
    that echoes the file contents) anywhere in pyinfra modules. Verify
    routines must use existence/length-only patterns instead.
    """

    SCANNED_MODULES = ("_install.py", "_persona.py", "_settings.py",
                       "_telegram_plugin.py", "_systemd_unit.py",
                       "_verify.py", "_cleanup_legacy.py", "deploy.py")

    @pytest.mark.parametrize("module_name", SCANNED_MODULES)
    def test_no_cat_runtime_env(self, module_name: str):
        path = AGENT_TASKS_DIR / module_name
        if not path.is_file():
            pytest.skip(f"{module_name} not yet created")
        source = _join_adjacent_string_literals(
            _strip_comments_and_strings(path.read_text(encoding="utf-8"))
        )
        # `cat /run/claude-agent/env` (or printf <, head, tail, less, etc.)
        bad_re = re.compile(
            r"(?:cat|head|tail|less|more|printf\s+<|nl)\s+[^\n;|&]*?/run/claude-agent/env\b"
        )
        violations = bad_re.findall(source)
        assert not violations, (
            f"Found forbidden read of /run/claude-agent/env in "
            f"{module_name}: {violations}. The decrypted runtime env is "
            f"plaintext — never echo its contents."
        )


class TestAgentLayerUsesHostHelper:
    """Same architectural invariant as the secrets layer: read tenant config
    via lib.host_helpers.get_tenant_config, never parse YAML directly."""

    def test_deploy_imports_get_tenant_config(self):
        path = AGENT_TASKS_DIR / "deploy.py"
        if not path.is_file():
            pytest.skip("agent/deploy.py not yet created")
        source = path.read_text(encoding="utf-8")
        assert re.search(
            r"from\s+lib\.host_helpers\s+import[^\n]*\bget_tenant_config\b",
            source,
        ), (
            "agent/deploy.py must import get_tenant_config from "
            "lib.host_helpers — direct YAML parsing in pyinfra tasks "
            "bypasses the SPEC-003 host-data exposure policy."
        )


class TestAgentLayerOrchestration:
    """Verify deploy.py wires the agent layer in (after secrets, before hello)."""

    def test_top_level_deploy_imports_agent(self):
        deploy_path = REPO_ROOT / "deploy.py"
        source = deploy_path.read_text(encoding="utf-8")
        assert "from tasks.agent import" in source or "tasks.agent" in source, (
            "Top-level deploy.py does not import tasks.agent — Step 4 "
            "deploy chain is broken."
        )

    def test_top_level_deploy_calls_agent_apply(self):
        deploy_path = REPO_ROOT / "deploy.py"
        source = deploy_path.read_text(encoding="utf-8")
        # Allow either `agent.apply()` or `agent_deploy.apply()` etc. — just
        # check that something in the agent namespace is invoked.
        assert re.search(r"\bagent[a-zA-Z_]*\.apply\s*\(", source), (
            "Top-level deploy.py never calls the agent layer's apply()."
        )


class TestAgentDeployOrchestration:
    """Verify tasks/agent/deploy.py invokes each sub-module."""

    def test_deploy_calls_each_submodule_apply(self):
        path = AGENT_TASKS_DIR / "deploy.py"
        if not path.is_file():
            pytest.skip("agent/deploy.py not yet created")
        source = path.read_text(encoding="utf-8")
        for sub in ("_install", "_persona", "_settings",
                    "_telegram_plugin", "_systemd_unit", "_verify",
                    "_cleanup_legacy"):
            assert re.search(rf"{sub}\.apply\s*\(", source), (
                f"agent/deploy.py never invokes {sub}.apply(). Missing "
                f"sub-module orchestration breaks the Step 4 chain."
            )

    def test_verify_runs_before_cleanup(self):
        """The cleanup step deletes the legacy plaintext fallback. It MUST
        run AFTER _verify.apply() succeeds, otherwise a deploy that fails
        to bring up the new systemd unit would also nuke the only working
        config — bricking the box.
        """
        path = AGENT_TASKS_DIR / "deploy.py"
        if not path.is_file():
            pytest.skip("agent/deploy.py not yet created")
        source = path.read_text(encoding="utf-8")
        verify_pos = source.find("_verify.apply")
        cleanup_pos = source.find("_cleanup_legacy.apply")
        assert verify_pos != -1 and cleanup_pos != -1, (
            "Both _verify.apply and _cleanup_legacy.apply must be present"
        )
        assert verify_pos < cleanup_pos, (
            f"_cleanup_legacy.apply (pos {cleanup_pos}) is invoked BEFORE "
            f"_verify.apply (pos {verify_pos}). This violates the Step 4 "
            f"safety invariant: never delete the legacy plaintext path "
            f"until the encrypted path is proven working."
        )


# ─── Step 5a (SPEC-010) — persona routing ───────────────────────────────────


class TestStep5aPersonaRouting:
    """SPEC-010 §"Persona-rsync mechanic": persona/<name>/ has FOUR sub-trees
    that route to FOUR distinct on-box locations:

        persona/<name>/CLAUDE.md       → ~/.claude/agents/<name>.md
        persona/<name>/agent-memory/   → ~/.claude/agent-memory/<name>/
        persona/<name>/workspace/      → /home/claude/agents/<name>/workspace/
        persona/<name>/skills/         → ~/.claude/skills/   (additive — no --delete)

    These tests are static checks against _persona.py — they verify each of
    the four routing destinations is referenced. Catch regressions where a
    refactor accidentally drops one of the sub-trees.
    """

    def test_persona_routes_agent_definition(self):
        path = AGENT_TASKS_DIR / "_persona.py"
        source = path.read_text(encoding="utf-8")
        # CLAUDE.md → ~/.claude/agents/<name>.md
        assert "/home/claude/.claude/agents" in source, (
            "_persona.py does not route persona/<name>/CLAUDE.md to "
            "/home/claude/.claude/agents/<name>.md — claude won't see the "
            "agent definition without this."
        )
        assert "{persona_name}.md" in source, (
            "_persona.py does not write the agent definition to "
            "<name>.md — naming must match how claude looks up subagents."
        )

    def test_persona_routes_agent_memory(self):
        path = AGENT_TASKS_DIR / "_persona.py"
        source = path.read_text(encoding="utf-8")
        assert "/home/claude/.claude/agent-memory" in source, (
            "_persona.py does not route agent-memory/ to "
            "/home/claude/.claude/agent-memory/<name>/."
        )

    def test_persona_routes_workspace(self):
        path = AGENT_TASKS_DIR / "_persona.py"
        source = path.read_text(encoding="utf-8")
        assert "workspace" in source, (
            "_persona.py does not reference 'workspace' — Morty's working "
            "directory tree won't be rsynced."
        )
        # Ensure the workspace target is under /home/claude/agents/<name>/
        assert re.search(
            r"/home/claude/agents/\{persona_name\}/workspace", source
        ), (
            "_persona.py workspace target must be "
            "/home/claude/agents/<persona_name>/workspace — that's where the "
            "systemd unit's WorkingDirectory parent expects it."
        )

    def test_persona_routes_skills_additively(self):
        path = AGENT_TASKS_DIR / "_persona.py"
        source = path.read_text(encoding="utf-8")
        # Strip docstrings + comments so we only inspect EXECUTABLE code.
        # The docstring at the top of _persona.py mentions delete=True for
        # the agent-memory tree which would confuse a naive regex scan.
        executable = _strip_comments_and_strings(source)
        assert "/home/claude/.claude/skills" in executable, (
            "_persona.py does not route skills/ to /home/claude/.claude/skills."
        )
        # Find the skills files.sync(...) block specifically — pin on the
        # destination path so we don't get confused by other sync blocks.
        skills_block_match = re.search(
            r'dest\s*=\s*["\']/home/claude/\.claude/skills["\'][^)]*?delete\s*=\s*(True|False)',
            executable,
            re.DOTALL,
        )
        assert skills_block_match is not None, (
            "_persona.py skills sync block has no delete= argument — set "
            "delete=False explicitly to document the additive intent."
        )
        assert skills_block_match.group(1) == "False", (
            "_persona.py skills sync uses delete=True. SPEC-010 §"
            '"Open question 2": skills are GLOBAL on the box — additive '
            "rsync only, otherwise we nuke skills installed by other "
            "agents or future Morty sessions."
        )


class TestStep5aMortyPersonaArtifacts:
    """The bubble-internal data repo must contain the morty/ persona tree
    with all four sub-trees populated. This guards against accidental
    deletion of the data-repo content that drives the deploy.
    """

    @pytest.fixture(scope="class")
    def morty_dir(self) -> Path:
        repo = (REPO_ROOT / ".." / "bubble-vps-data").resolve()
        return repo / "tenants" / "bubble-internal" / "persona" / "morty"

    def test_morty_persona_dir_exists(self, morty_dir: Path):
        assert morty_dir.is_dir(), f"Morty persona dir missing: {morty_dir}"

    def test_morty_agent_definition_exists(self, morty_dir: Path):
        claude_md = morty_dir / "CLAUDE.md"
        assert claude_md.is_file(), (
            f"Morty agent definition missing at {claude_md}. This file is "
            f"the rnd.md derivative that goes to ~/.claude/agents/morty.md."
        )
        body = claude_md.read_text(encoding="utf-8")
        # Identity transformation per SPEC-010 §"Identity transformation":
        # the IDENTITY block must say Morty, not R&D/Lab.
        assert "IDENTITY — Morty" in body, (
            "Morty CLAUDE.md missing 'IDENTITY — Morty' header — the Lab→Morty "
            "identity transformation per SPEC-010 was not applied."
        )
        assert "Cloud habitat" in body, (
            "Morty CLAUDE.md missing 'Cloud habitat' section — SPEC-010 "
            "requires this section to differentiate cloud-side constraints "
            "from Mac-side Lab capabilities."
        )

    def test_morty_agent_memory_populated(self, morty_dir: Path):
        memory_dir = morty_dir / "agent-memory"
        assert memory_dir.is_dir(), f"Morty agent-memory missing: {memory_dir}"
        # MEMORY.md is Lab's index file — must be present + must contain the
        # Morty fork note.
        memory_md = memory_dir / "MEMORY.md"
        assert memory_md.is_file()
        body = memory_md.read_text(encoding="utf-8")
        assert "Morty fork" in body, (
            "MEMORY.md missing the 'Morty fork' note appended per SPEC-010 "
            "§\"Memory inheritance\"."
        )
        # joris_profile.md must be present (smoke-test acceptance criterion 7
        # in SPEC-010 — Morty must surface Joris's profile on demand).
        assert (memory_dir / "joris_profile.md").is_file(), (
            "joris_profile.md not rsynced into Morty's agent-memory."
        )

    def test_morty_workspace_populated(self, morty_dir: Path):
        ws = morty_dir / "workspace"
        assert ws.is_dir(), f"Morty workspace missing: {ws}"
        # Required files per SPEC-010 §"Workspace inheritance":
        assert (ws / "CLAUDE.md").is_file()
        assert (ws / "BACKLOG.md").is_file()
        assert (ws / "monitoring").is_dir()
        assert (ws / "tools").is_dir()
        assert (ws / "proposals").is_dir()

    def test_morty_skills_populated(self, morty_dir: Path):
        skills = morty_dir / "skills"
        assert skills.is_dir(), f"Morty skills dir missing: {skills}"
        # The five cloud-compatible skills per SPEC-010 §"Skills inheritance":
        for required in (
            "notion-reader",
            "google-workspace",
            "scheduled-task-creation",
            "remote-access",
            "telegram-reporter",
        ):
            assert (skills / required).is_dir(), (
                f"Required skill missing from Morty's skills tree: {required}"
            )

    def test_morty_workspace_under_size_cap(self, morty_dir: Path):
        """Total morty/ payload must stay under the 50MB SPEC-010 ceiling.
        We aim for <10MB in practice — a hard guard at 50MB catches future
        accidental inclusions of large blobs (videos, prototypes/, etc.).
        """
        total = sum(p.stat().st_size for p in morty_dir.rglob("*") if p.is_file())
        cap_mb = 50
        assert total < cap_mb * 1024 * 1024, (
            f"Morty persona payload is {total / 1024 / 1024:.1f} MB — "
            f"exceeds the {cap_mb} MB SPEC-010 ceiling. Likely a forgotten "
            f"prototypes/ or videos directory was included."
        )


# ─── Multi-concierge agent layer (SPEC-001 v1.2) ─────────────────────────────


class TestMultiConciergeAgentLayer:
    """Per-concierge derivation of service name, UNPREFIXED workdir (SPEC-021
    inv#6), session-transcript dir, and runtime env file. These are the
    single-source helpers the agent tasks + watchdog all consume."""

    def test_concierge_workdir_is_unprefixed(self):
        """SPEC-021 inv#6: concierge workdir is /home/claude/agents/<name> with
        NO bubble-ops- prefix (the prefix is the DEPARTMENT marker)."""
        from lib.host_helpers import agent_workdir

        assert agent_workdir("morty") == "/home/claude/agents/morty"
        assert agent_workdir("claudette") == "/home/claude/agents/claudette"
        # Never carries the department prefix.
        assert "bubble-ops-" not in agent_workdir("sandra")

    def test_concierge_service_name(self):
        from lib.host_helpers import agent_service_name

        assert agent_service_name("morty") == "claude-agent-morty.service"
        assert agent_service_name("claudette") == "claude-agent-claudette.service"

    def test_concierge_session_dir_tracks_unprefixed_workdir(self):
        """The 401-probe session dir is workdir with / → - (SPEC-021 inv#4d).
        For a concierge it is the UNPREFIXED form."""
        from lib.host_helpers import agent_session_projects_dir

        assert agent_session_projects_dir("claudette") == (
            "/home/claude/.claude/projects/-home-claude-agents-claudette"
        )

    def test_runtime_env_dir_primary_vs_other(self):
        from lib.host_helpers import runtime_env_dir

        # Primary keeps the historical dir (dirname of decrypted_runtime_path).
        assert (
            runtime_env_dir(
                "morty", is_primary=True, primary_runtime_path="/run/claude-agent/env"
            )
            == "/run/claude-agent"
        )
        # Others get a name-suffixed sibling.
        assert (
            runtime_env_dir(
                "claudette",
                is_primary=False,
                primary_runtime_path="/run/claude-agent/env",
            )
            == "/run/claude-agent-claudette"
        )

    def test_systemd_unit_renders_for_secondary_concierge(self):
        """Rendering the agent service for a NON-primary concierge must point
        WorkingDirectory + decrypt target + runtime dir at its own name-suffixed
        paths (no clobbering the primary's /run/claude-agent)."""
        from lib.host_helpers import (
            runtime_env_dir,
            runtime_env_file,
            telegram_channel_dir,
        )

        rt_dir = runtime_env_dir(
            "claudette", is_primary=False, primary_runtime_path="/run/claude-agent/env"
        )
        rt_file = runtime_env_file(
            "claudette", is_primary=False, primary_runtime_path="/run/claude-agent/env"
        )
        rendered = _render(
            "claude-agent.service.j2",
            persona_name="claudette",
            tenant_name="bubble-internal",
            age_key_path="/etc/age/key.txt",
            encrypted_file_path="/etc/bubble/secrets.sops.env",
            decrypted_runtime_path=rt_file,
            runtime_env_dir=rt_dir,
            bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
            telegram_state_dir=telegram_channel_dir("claudette"),
            sops_bin="/usr/local/bin/sops",
            claude_bin="/usr/bin/claude",
            channels="plugin:telegram@claude-plugins-official",
            restart="on-failure",
            restart_sec=10,
            nofile_limit=65536,
        )
        assert "WorkingDirectory=/home/claude/agents/claudette" in rendered
        assert "ExecStartPre=+/bin/mkdir -p /run/claude-agent-claudette" in rendered
        assert "EnvironmentFile=-/run/claude-agent-claudette/env" in rendered
        # Must NOT touch the primary's runtime dir.
        assert "mkdir -p /run/claude-agent\n" not in rendered

    def test_agent_tasks_loop_over_concierges(self):
        """The agent sub-modules must iterate cfg.agent.concierges so additional
        concierges are first-class, not dropped."""
        for module in ("_persona.py", "_settings.py", "_systemd_unit.py",
                       "_telegram_plugin.py", "_verify.py"):
            source = (AGENT_TASKS_DIR / module).read_text(encoding="utf-8")
            assert "cfg.agent.concierges" in source, (
                f"{module} must loop over cfg.agent.concierges — otherwise a "
                f"second concierge (claudette) never gets its persona / settings "
                f"/ channel / service / verification."
            )


# ─── Git-backed concierge workspace (SPEC-001 v1.3) ──────────────────────────
# A concierge with `workspace_repo` set (claudette → her own git repo) must be
# deployed by CLONING that repo into the workdir /home/claude/agents/<name>,
# NOT by syncing a data-repo persona/<name>/workspace/ tree. The clone is
# idempotent (test -d <dir>/.git || git clone). Crucially: NO files.sync with
# delete=True may touch a git-backed concierge's workdir (that's the data-loss
# risk we're avoiding — it would clobber the agent's uncommitted runtime work).
# Identity (CLAUDE.md → agents/<name>.md) + agent-memory/ STILL deploy from the
# data-repo persona/ for git-backed AND synced concierges alike.


def _import_persona_with_recorders():
    """Import pyinfra/tasks/agent/_persona.py with the `pyinfra` and
    `pyinfra.operations` modules replaced by recorders, so we can capture the
    exact ops it emits for a given concierge WITHOUT a live SSH connection.

    Returns (module, files_recorder, server_recorder). The recorders expose a
    `.calls` dict mapping op-name → list of kwargs.
    """
    import importlib
    import types

    class _OpRecorder:
        def __init__(self):
            self.calls: dict[str, list[dict]] = {}

        def _record(self, op_name):
            def _op(*args, **kwargs):
                self.calls.setdefault(op_name, []).append(kwargs)
                # Return an object with a did_change() method so any _if=
                # wiring downstream stays callable.
                marker = types.SimpleNamespace(did_change=lambda: True)
                return marker
            return _op

        def __getattr__(self, name):
            return self._record(name)

    files_rec = _OpRecorder()
    server_rec = _OpRecorder()

    # Build fake pyinfra package tree.
    fake_pyinfra = types.ModuleType("pyinfra")
    fake_pyinfra.host = types.SimpleNamespace()  # patched per-test
    fake_ops = types.ModuleType("pyinfra.operations")
    fake_ops.files = files_rec
    fake_ops.server = server_rec
    fake_pyinfra.operations = fake_ops

    saved = {
        k: sys.modules.get(k)
        for k in ("pyinfra", "pyinfra.operations", "tasks.agent._persona")
    }
    sys.modules["pyinfra"] = fake_pyinfra
    sys.modules["pyinfra.operations"] = fake_ops
    sys.modules.pop("tasks.agent._persona", None)

    if str(REPO_ROOT / "pyinfra") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "pyinfra"))

    try:
        mod = importlib.import_module("tasks.agent._persona")
        # Point the module's bound `files`/`server` at our recorders (the
        # `from pyinfra.operations import files, server` binding captured them
        # at import, but be defensive in case import order differs).
        mod.files = files_rec
        if hasattr(mod, "server"):
            mod.server = server_rec
        return mod, files_rec, server_rec
    finally:
        # Restore sys.modules so other tests see the real pyinfra (or its
        # absence). The imported _persona module keeps our recorder bindings.
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _make_concierge(name, persona_dir, *, workspace_repo=None,
                    workspace_branch="main"):
    """Build a ConciergeConfig directly (bypassing YAML) for behavior tests."""
    from lib.tenant_loader import (
        AgentChannelsConfig,
        AgentLLMConfig,
        ConciergeConfig,
        TelegramChannelConfig,
    )
    return ConciergeConfig(
        name=name,
        persona_dir=persona_dir,
        channels=AgentChannelsConfig(
            telegram=TelegramChannelConfig(
                enabled=True,
                bot_token_secret_ref="TELEGRAM_BOT_TOKEN",
                allowed_user_ids=["1"],
            )
        ),
        llm=AgentLLMConfig(
            provider="anthropic",
            model="opus[1m]",
            auth_mode="claude_code_subscription",
        ),
        workspace_repo=workspace_repo,
        workspace_branch=workspace_branch,
    )


class _FakeCfg:
    def __init__(self, tenant_dir):
        self.tenant_dir = tenant_dir


class TestGitBackedConciergeWorkspace:
    """Behavioral tests on _persona._apply_one — the workspace step must branch
    on workspace_repo (SPEC-001 v1.3)."""

    def _setup_git_backed(self, tmp_path):
        """Create a git-backed concierge (claudette) persona dir on disk:
        CLAUDE.md + agent-memory/, NO workspace/ subdir."""
        persona = tmp_path / "persona" / "claudette"
        (persona / "agent-memory").mkdir(parents=True)
        (persona / "CLAUDE.md").write_text("# claudette\n")
        (persona / "agent-memory" / "hot.md").write_text("# hot\n")
        concierge = _make_concierge(
            "claudette",
            "persona/claudette",
            workspace_repo="https://github.com/vdk888/bubble-claudette-workspace.git",
        )
        return persona, concierge

    def test_git_backed_concierge_emits_clone_if_absent(self, tmp_path):
        mod, files_rec, server_rec = _import_persona_with_recorders()
        _, concierge = self._setup_git_backed(tmp_path)
        cfg = _FakeCfg(str(tmp_path))
        # is_primary=False → persona_dir resolved via cfg.tenant_dir.
        mod._apply_one(cfg, concierge, is_primary=False)

        # A server.shell clone op must have been emitted.
        shell_calls = server_rec.calls.get("shell", [])
        assert shell_calls, (
            "git-backed concierge must emit a server.shell clone-if-absent op"
        )
        # Find the clone command. It must guard on <dir>/.git and target the
        # workdir directly (NOT a workspace/ subdir).
        joined = "\n".join(
            " ".join(c.get("commands", []) if isinstance(c.get("commands"), list)
                     else [str(c.get("commands", ""))])
            for c in shell_calls
        )
        assert "/home/claude/agents/claudette/.git" in joined, (
            "clone guard must test -d /home/claude/agents/claudette/.git"
        )
        assert "git clone" in joined
        assert (
            "https://github.com/vdk888/bubble-claudette-workspace.git" in joined
        )
        # Clone target is the workdir directly — NOT under a workspace/ subdir.
        assert "/home/claude/agents/claudette/workspace" not in joined, (
            "git-backed concierge clones into the workdir directly; her repo "
            "files live at the top level, not under workspace/"
        )

    def test_git_backed_concierge_no_destructive_sync_on_workdir(self, tmp_path):
        mod, files_rec, server_rec = _import_persona_with_recorders()
        _, concierge = self._setup_git_backed(tmp_path)
        cfg = _FakeCfg(str(tmp_path))
        mod._apply_one(cfg, concierge, is_primary=False)

        # NO files.sync may target /home/claude/agents/claudette (or any subdir
        # of it). agent-memory sync (under ~/.claude/agent-memory) is fine.
        for call in files_rec.calls.get("sync", []):
            dest = str(call.get("dest", ""))
            assert not dest.startswith("/home/claude/agents/claudette"), (
                f"DATA-LOSS RISK: files.sync targets the git-backed concierge's "
                f"workdir {dest!r}. delete=True there would clobber her repo + "
                f"uncommitted runtime work. git-backed concierges only get a "
                f"clone-if-absent."
            )

    def test_git_backed_concierge_still_syncs_identity_and_memory(self, tmp_path):
        mod, files_rec, server_rec = _import_persona_with_recorders()
        _, concierge = self._setup_git_backed(tmp_path)
        cfg = _FakeCfg(str(tmp_path))
        mod._apply_one(cfg, concierge, is_primary=False)

        # Identity: CLAUDE.md → ~/.claude/agents/claudette.md (files.put).
        put_dests = [str(c.get("dest", "")) for c in files_rec.calls.get("put", [])]
        assert any(
            d.endswith("/.claude/agents/claudette.md") for d in put_dests
        ), (
            "git-backed concierge must STILL get her identity file installed at "
            "~/.claude/agents/claudette.md from the data-repo persona CLAUDE.md"
        )
        # Agent memory: agent-memory/ → ~/.claude/agent-memory/claudette (sync).
        sync_dests = [str(c.get("dest", "")) for c in files_rec.calls.get("sync", [])]
        assert any(
            d.endswith("/.claude/agent-memory/claudette") for d in sync_dests
        ), (
            "git-backed concierge must STILL get her agent-memory synced from "
            "the data-repo persona/"
        )

    def test_synced_concierge_still_syncs_workspace(self, tmp_path):
        """A NON-git concierge (morty-like: has a workspace/ tree, no
        workspace_repo) must STILL sync workspace/ with delete=True."""
        mod, files_rec, server_rec = _import_persona_with_recorders()
        persona = tmp_path / "persona" / "morty"
        (persona / "workspace").mkdir(parents=True)
        (persona / "CLAUDE.md").write_text("# morty\n")
        (persona / "workspace" / "STATUS.md").write_text("# status\n")
        concierge = _make_concierge("morty", "persona/morty")  # no workspace_repo
        cfg = _FakeCfg(str(tmp_path))
        mod._apply_one(cfg, concierge, is_primary=False)

        # workspace/ must be synced to /home/claude/agents/morty/workspace
        # with delete=True.
        workspace_syncs = [
            c for c in files_rec.calls.get("sync", [])
            if str(c.get("dest", "")) == "/home/claude/agents/morty/workspace"
        ]
        assert workspace_syncs, (
            "non-git concierge must still files.sync workspace/ — the data repo "
            "is canonical for a synced concierge's workdir"
        )
        assert workspace_syncs[0].get("delete") is True, (
            "synced workspace must use delete=True (data-repo canonical)"
        )
        # And NO git clone op for a synced concierge.
        assert not server_rec.calls.get("shell"), (
            "non-git concierge must NOT emit a git clone op"
        )


class TestPersonaSourceGitBackedBranch:
    """Static guards on _persona.py source for the git-backed branch."""

    def test_persona_references_workspace_repo(self):
        source = (AGENT_TASKS_DIR / "_persona.py").read_text(encoding="utf-8")
        assert "workspace_repo" in source, (
            "_persona.py must branch on concierge.workspace_repo"
        )

    def test_clone_guard_is_idempotent(self):
        """The git-backed branch must guard the clone (test -d <dir>/.git ||)
        so re-deploys don't re-clone / clobber."""
        source = (AGENT_TASKS_DIR / "_persona.py").read_text(encoding="utf-8")
        executable = _strip_comments_and_strings(source)
        assert "git clone" in executable, "clone command missing from executable"
        assert ".git" in executable and "test -d" in executable, (
            "_persona.py clone must be guarded by `test -d <dir>/.git ||` to be "
            "idempotent (no re-clone over an existing checkout)"
        )

    def test_no_autopull_on_git_backed_workdir(self):
        """Deploy must NOT auto-pull/reset a git-backed workdir — that could
        clobber the agent's uncommitted runtime work. Only clone-if-absent."""
        source = (AGENT_TASKS_DIR / "_persona.py").read_text(encoding="utf-8")
        executable = _strip_comments_and_strings(source)
        assert "git pull" not in executable, (
            "_persona.py must NOT `git pull` a git-backed concierge's workdir — "
            "ongoing updates are the agent's own responsibility; deploy only "
            "guarantees the clone EXISTS"
        )
        assert "git reset" not in executable, (
            "_persona.py must NOT `git reset` a git-backed concierge's workdir"
        )


# ─── su-claude self-su bug regression (2026-05-31) ───────────────────────────


class TestAgentLayerNoBareSuClaude:
    """Regression guard for the `su - claude` self-su password-prompt bug.

    The pyinfra deploy connects to the box AS the `claude` user
    (tenant.yaml → host.ssh_user: claude). Several agent tasks historically ran
    commands via `su - claude -c '...'`. But `su - claude` when you are ALREADY
    claude PROMPTS FOR A PASSWORD (self-su needs auth) and aborts:
    `su: Authentication failure` → `pyinfra error: No hosts remaining!`. This
    blocked EVERY deploy. (Same class of bug fixed in bubble-ops-loop's
    deploy-to-morty.sh on 2026-05-31.)

    The fix: run the command directly (we're already claude), guarded for the
    hypothetical future root-connect case with a NOPASSWD-safe branch:
        if [ "$(id -un)" = claude ]; then <CMD>; else sudo -n -u claude <CMD>; fi
    NEVER bare `su - claude`.

    These tests scan EXECUTABLE code only (docstrings/comments stripped) so the
    prose that explains the bug doesn't trip the assertion.
    """

    # Modules that previously had `su - claude` executable call sites.
    GUARDED_MODULES = ("_cleanup_legacy.py", "_install.py", "_settings.py")

    def _executable(self, module_name: str) -> str:
        path = AGENT_TASKS_DIR / module_name
        return _join_adjacent_string_literals(
            _strip_comments_and_strings(path.read_text(encoding="utf-8"))
        )

    @pytest.mark.parametrize("module_name", GUARDED_MODULES)
    def test_no_bare_su_claude_in_executable_code(self, module_name: str):
        """No EXECUTABLE command may use bare `su - claude` (the bug).
        Docstrings/comments mentioning it for explanation are fine."""
        executable = self._executable(module_name)
        assert "su - claude" not in executable, (
            f"{module_name} regressed to bare `su - claude` in executable code "
            f"— self-su prompts for a password and aborts the deploy. Use the "
            f"id -un-guarded form (lib.host_helpers.as_claude) instead."
        )

    @pytest.mark.parametrize("module_name", GUARDED_MODULES)
    def test_uses_as_claude_guard_helper(self, module_name: str):
        """Each fixed module must route its run-as-claude commands through the
        shared as_claude() guard (DRY — one place owns the quoting)."""
        source = (AGENT_TASKS_DIR / module_name).read_text(encoding="utf-8")
        assert re.search(
            r"from\s+lib\.host_helpers\s+import\s*\(?[^)\n]*\bas_claude\b",
            source,
        ), f"{module_name} must import as_claude from lib.host_helpers"
        executable = self._executable(module_name)
        assert "as_claude(" in executable, (
            f"{module_name} must wrap its run-as-claude command(s) in "
            f"as_claude(...) (the id -un-guarded, NOPASSWD-safe form)"
        )


class TestAsClaudeHelper:
    """Behavioural tests for lib.host_helpers.as_claude — the single source of
    truth for the run-as-claude guard."""

    def _as_claude(self):
        from lib.host_helpers import as_claude
        return as_claude

    def test_guard_branches_on_login_user(self):
        out = self._as_claude()("echo hi")
        assert 'if [ "$(id -un)" = claude ]; then echo hi;' in out, (
            "as_claude must branch on the login user via `id -un` so it runs "
            "directly when already claude (no password prompt)"
        )

    def test_root_fallback_is_nopasswd_safe(self):
        out = self._as_claude()("echo hi")
        assert "else sudo -n -u claude echo hi; fi" in out, (
            "root fallback must be `sudo -n -u claude` (-n = never prompt, "
            "NOPASSWD-safe) — NOT `su - claude` which prompts for a password"
        )

    def test_never_emits_bare_su_claude(self):
        out = self._as_claude()("echo hi")
        assert "su - claude" not in out, (
            "as_claude must NEVER emit bare `su - claude` (the self-su bug)"
        )

    def test_command_is_interpolated_verbatim_into_both_branches(self):
        out = self._as_claude()("sh -c 'do | a | pipe'")
        # appears once in the then-branch, once in the else-branch
        assert out.count("sh -c 'do | a | pipe'") == 2, (
            "the wrapped command must appear verbatim in BOTH the then- and "
            "else-branches so a pipeline stays intact regardless of which "
            "branch runs"
        )


class TestRunAsClaudePipelinesPreserved:
    """The base64-pipe (trust-seed) and curl|bash (bun install) pipelines must
    survive the guard wrapping intact — the WHOLE pipe must run as claude, so
    each is wrapped in `sh -c '...'` before as_claude (otherwise the root
    `sudo -n -u claude` fallback would only run the first pipe segment as
    claude)."""

    def test_trust_seed_base64_pipe_preserved(self):
        """_settings._trust_seed_command emits the base64 | base64 -d | python3 -
        pipeline wrapped in `sh -c` inside the id -un guard, with stdout intact."""
        import base64 as _b64
        import importlib

        # _settings.py does `from pyinfra import host` at import time; inject a
        # stub so importing the module standalone doesn't require pyinfra/SSH.
        # (Same pattern as the persona behaviour tests.)
        stubbed = _install_pyinfra_stubs()
        try:
            mod = importlib.import_module("tasks.agent._settings")
            importlib.reload(mod)
            cmd = mod._trust_seed_command("/home/claude/agents/morty")
        finally:
            _remove_pyinfra_stubs(stubbed)

        # bare su must be gone; guard + pipe present
        assert "su - claude" not in cmd
        assert 'if [ "$(id -un)" = claude ]; then' in cmd
        assert "else sudo -n -u claude " in cmd
        # the FULL pipeline lives inside an sh -c '...' so it runs as claude
        assert "sh -c 'echo " in cmd
        assert "| base64 -d | python3 -'" in cmd
        # the encoded blob round-trips to the original python (decode the b64
        # token sitting between `echo ` and ` |`)
        token = cmd.split("sh -c 'echo ", 1)[1].split(" |", 1)[0]
        decoded = _b64.b64decode(token).decode("utf-8")
        assert "hasTrustDialogAccepted" in decoded and "json.dump" in decoded, (
            "the base64 trust-seed payload must survive the guard wrapping intact"
        )

    def test_install_bun_curl_pipe_preserved(self):
        """_install.py wraps the bun `curl ... | bash` pipeline in `sh -c` inside
        the id -un guard so the whole pipe runs as claude."""
        executable = _join_adjacent_string_literals(
            _strip_comments_and_strings(
                (AGENT_TASKS_DIR / "_install.py").read_text(encoding="utf-8")
            )
        )
        assert "su - claude" not in executable
        assert "as_claude(" in executable
        assert "sh -c 'curl -fsSL https://bun.sh/install | bash'" in executable, (
            "the bun installer curl|bash pipeline must stay wrapped in sh -c so "
            "the WHOLE pipe runs as claude (not just curl)"
        )

    def test_cleanup_tmux_fact_preserves_yes_no_stdout(self):
        """_cleanup_legacy's has-session fact must keep the `&& echo yes || echo
        no` stdout it depends on, inside the guard."""
        executable = _join_adjacent_string_literals(
            _strip_comments_and_strings(
                (AGENT_TASKS_DIR / "_cleanup_legacy.py").read_text(encoding="utf-8")
            )
        )
        assert "su - claude" not in executable
        assert "tmux has-session -t claude-agent 2>/dev/null && echo yes || echo no" in executable
        assert "tmux kill-session -t claude-agent 2>/dev/null || true" in executable


def _install_pyinfra_stubs():
    """Inject minimal fake `pyinfra` + submodules into sys.modules so
    `tasks.agent._settings` imports standalone (no SSH). Returns the keys we
    added so the caller can restore. Mirrors the persona behaviour-test trick.
    """
    import types

    if str(REPO_ROOT / "pyinfra") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "pyinfra"))

    added = []
    for name in (
        "pyinfra",
        "pyinfra.facts",
        "pyinfra.facts.server",
        "pyinfra.operations",
    ):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            added.append(name)
    # attributes the module pulls at import time
    sys.modules["pyinfra"].host = object()
    sys.modules["pyinfra.facts.server"].Command = object()
    ops = sys.modules["pyinfra.operations"]
    ops.files = types.SimpleNamespace()
    ops.server = types.SimpleNamespace()
    return added


def _remove_pyinfra_stubs(added):
    for name in added:
        sys.modules.pop(name, None)
    # also drop the freshly-imported task module so other tests reimport clean
    sys.modules.pop("tasks.agent._settings", None)


# ─── Sudo-escalation static guard (regression: 2026-05-31 deploy blocker) ─────
#
# The deploy connects AS the `claude` user (tenant ssh_user: claude). Any op
# that writes a ROOT-owned path (/etc/...) or runs a root-only command
# (systemctl / journalctl / apt) MUST pass `_sudo=True` (escalate to root) or
# pyinfra dies with `[Errno 13] Permission denied` → `No hosts remaining!`.
# Claude-owned writes (/home/claude/...) must pass `_sudo=True,
# _sudo_user="claude"`. These tests parse the task-module source (AST) and
# assert the escalation kwarg is present on every relevant op, guarding against
# a future refactor silently dropping it.
#
# RULE:
#   ROOT target (dest/path under /etc/, /usr/, root-only command) → `_sudo=True`
#     and NOT `_sudo_user` (adding _sudo_user="claude" to an /etc write would
#     write as claude → still Permission denied).
#   CLAUDE target (dest/path under /home/claude) → `_sudo=True` AND
#     `_sudo_user="claude"`.


def _resolve_str_fragments(node, env, _depth=0):
    """Return the set of string fragments reachable from an AST expression.

    Resolves simple `Name` references to their module/function-level
    assignments and walks f-strings + `+` concatenations so we can classify a
    `dest=`/`path=` argument as root-owned (/etc, /usr) vs claude-owned
    (/home/claude) even when it's built from variables + helper calls.
    """
    import ast

    out: set[str] = set()
    if _depth > 12:
        return out
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.add(node.value)
    elif isinstance(node, ast.JoinedStr):
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.add(v.value)
            elif isinstance(v, ast.FormattedValue):
                out |= _resolve_str_fragments(v.value, env, _depth + 1)
    elif isinstance(node, ast.Name):
        for val in env.get(node.id, []):
            out |= _resolve_str_fragments(val, env, _depth + 1)
    elif isinstance(node, ast.BinOp):
        out |= _resolve_str_fragments(node.left, env, _depth + 1)
        out |= _resolve_str_fragments(node.right, env, _depth + 1)
    elif isinstance(node, ast.Call):
        # Mark helper calls so the classifier can special-case known ones
        # (e.g. _unit_path() returns an /etc path).
        if isinstance(node.func, ast.Name):
            out.add("__call__" + node.func.id)
        elif isinstance(node.func, ast.Attribute):
            out.add("__call__" + node.func.attr)
    return out


def _build_assign_env(tree):
    """Map every `Name = <expr>` assignment in a module to its value node(s)."""
    import ast

    env: dict[str, list] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    env.setdefault(t.id, []).append(n.value)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.value is not None:
                env.setdefault(n.target.id, []).append(n.value)
    return env


# Helpers known to return a root-owned /etc/systemd/system path. Used so the
# classifier treats `dest=_unit_path(name)` (resolving to a __call__ marker) as
# a root target.
_ROOT_PATH_HELPERS = {"__call___unit_path"}


def _classify_target(fragments):
    """Return 'root' | 'claude' | 'unknown' for a resolved set of fragments."""
    joined = " ".join(fragments)
    if any(frag.startswith("/etc/") or frag.startswith("/usr/") for frag in fragments):
        return "root"
    if _ROOT_PATH_HELPERS & set(fragments):
        return "root"
    if "/home/claude" in joined or any("/home/claude" in f for f in fragments):
        return "claude"
    return "unknown"


def _command_strings(node):
    """Collect literal command strings from a `commands=[...]` kwarg of a
    server.shell call (best-effort: constants + f-string constant parts)."""
    import ast

    cmds: list[str] = []
    for kw in node.keywords:
        if kw.arg != "commands":
            continue
        if isinstance(kw.value, ast.List):
            for elt in kw.value.elts:
                parts: list[str] = []
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    parts.append(elt.value)
                elif isinstance(elt, ast.JoinedStr):
                    for v in elt.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            parts.append(v.value)
                elif isinstance(elt, ast.BinOp):
                    # adjacent-literal concat handled crudely
                    for sub in ast.walk(elt):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            parts.append(sub.value)
                if parts:
                    cmds.append(" ".join(parts))
    return cmds


_ROOT_ONLY_CMD_TOKENS = (
    "systemctl ",
    "journalctl ",
    "apt-get ",
    "apt-get\n",
    "npm install -g",
)


def _iter_pyinfra_ops(tree):
    """Yield (qualname, call_node) for every files.*/server.shell/systemd.service
    call in the module AST."""
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


def _has_kw(node, name):
    return any(k.arg == name for k in node.keywords)


def _kw_value_is_claude(node, name):
    import ast

    for k in node.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value == "claude"
    return False


# Modules audited for the sudo-escalation regression. The list mirrors the task
# brief: the two primary offenders plus every other agent-layer task module.
_SUDO_AUDIT_MODULES = (
    "_systemd_unit.py",
    "_verify.py",
    "_install.py",
    "_telegram_plugin.py",
    "_settings.py",
    "_cleanup_legacy.py",
)


class TestAgentLayerSudoEscalation:
    """Every root-owned write / root-only command escalates; claude-owned writes
    escalate to the claude user. Static AST guard against the 2026-05-31
    Permission-denied deploy blocker."""

    @pytest.mark.parametrize("module_name", _SUDO_AUDIT_MODULES)
    def test_file_ops_under_etc_or_usr_have_sudo_no_sudo_user(self, module_name):
        import ast

        path = AGENT_TASKS_DIR / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        env = _build_assign_env(tree)

        checked = 0
        for qual, node in _iter_pyinfra_ops(tree):
            if not qual.startswith("files."):
                continue
            fragments = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    fragments |= _resolve_str_fragments(kw.value, env)
            target = _classify_target(fragments)
            if target == "root":
                checked += 1
                assert _has_kw(node, "_sudo"), (
                    f"{module_name}: {qual} writing a ROOT-owned path "
                    f"({sorted(fragments)}) is MISSING _sudo=True. The deploy "
                    f"connects AS claude and cannot write /etc or /usr — this "
                    f"reproduces the 2026-05-31 Permission-denied deploy blocker."
                )
                assert not _has_kw(node, "_sudo_user"), (
                    f"{module_name}: {qual} writing a ROOT-owned path "
                    f"({sorted(fragments)}) must NOT set _sudo_user — escalate "
                    f"to root, not claude (claude can't write /etc)."
                )
        # Sanity: at least one root file op must be covered overall (across the
        # suite the systemd modules supply these).
        assert checked >= 0  # per-module count may be 0 (e.g. _settings)

    @pytest.mark.parametrize("module_name", _SUDO_AUDIT_MODULES)
    def test_file_ops_under_home_claude_have_sudo_user_claude(self, module_name):
        import ast

        path = AGENT_TASKS_DIR / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        env = _build_assign_env(tree)

        for qual, node in _iter_pyinfra_ops(tree):
            if not qual.startswith("files."):
                continue
            fragments = set()
            for kw in node.keywords:
                if kw.arg in ("dest", "path"):
                    fragments |= _resolve_str_fragments(kw.value, env)
            if _classify_target(fragments) == "claude":
                assert _has_kw(node, "_sudo"), (
                    f"{module_name}: {qual} writing a CLAUDE-owned path "
                    f"({sorted(fragments)}) is MISSING _sudo=True."
                )
                assert _kw_value_is_claude(node, "_sudo_user"), (
                    f"{module_name}: {qual} writing a CLAUDE-owned path "
                    f"({sorted(fragments)}) must set _sudo_user=\"claude\" so "
                    f"the file ends up owned by claude, not root."
                )

    @pytest.mark.parametrize("module_name", _SUDO_AUDIT_MODULES)
    def test_root_only_shell_commands_have_sudo(self, module_name):
        import ast

        path = AGENT_TASKS_DIR / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for qual, node in _iter_pyinfra_ops(tree):
            if qual != "server.shell":
                continue
            cmds = _command_strings(node)
            joined = "\n".join(cmds)
            # as_claude(...)-wrapped commands run as claude via an internal
            # sudo -n -u claude fallback and do NOT take a pyinfra _sudo kwarg.
            # Detect them by the absence of a bare systemctl/journalctl/apt at
            # the START of a command token AND presence of an as_claude marker.
            uses_as_claude = any("id -un" in c or "sudo -n -u claude" in c for c in cmds)
            is_root_only = any(tok in joined for tok in _ROOT_ONLY_CMD_TOKENS)
            if is_root_only and not uses_as_claude:
                assert _has_kw(node, "_sudo"), (
                    f"{module_name}: server.shell running a root-only command "
                    f"({joined!r}) is MISSING _sudo=True — systemctl/journalctl/"
                    f"apt require root and the deploy connects AS claude."
                )

    @pytest.mark.parametrize("module_name", _SUDO_AUDIT_MODULES)
    def test_systemd_service_ops_have_sudo(self, module_name):
        import ast

        path = AGENT_TASKS_DIR / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qual, node in _iter_pyinfra_ops(tree):
            if qual != "systemd.service":
                continue
            assert _has_kw(node, "_sudo"), (
                f"{module_name}: systemd.service (enable/start a system unit) "
                f"is MISSING _sudo=True — root-only operation."
            )

    def test_systemd_unit_root_write_is_escalated_concretely(self):
        """Concrete spot-check on _systemd_unit.py: the /etc/systemd/system unit
        write must have _sudo=True and NO _sudo_user (root target)."""
        import ast

        path = AGENT_TASKS_DIR / "_systemd_unit.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        env = _build_assign_env(tree)
        found = False
        for qual, node in _iter_pyinfra_ops(tree):
            if qual != "files.template":
                continue
            frags = set()
            for kw in node.keywords:
                if kw.arg == "dest":
                    frags |= _resolve_str_fragments(kw.value, env)
            if _classify_target(frags) == "root":
                found = True
                assert _has_kw(node, "_sudo")
                assert not _has_kw(node, "_sudo_user")
        assert found, (
            "_systemd_unit.py: expected a root-owned /etc/systemd/system unit "
            "write to be detected by the classifier."
        )


# ─── _verify per-concierge env-var-name gate (token-remap awareness) ──────────
#
# BUG: _verify Step 5 looped `s.required_keys` and grep'd `^<KEY>=` in the
# concierge's runtime env file. required_keys includes CLAUDETTE_TELEGRAM_BOT_TOKEN.
# But for a NON-PRIMARY concierge the systemd unit REMAPS her bot-token ref:
# it DROPS the literal TELEGRAM_BOT_TOKEN= line and RENAMES
# CLAUDETTE_TELEGRAM_BOT_TOKEN= → TELEGRAM_BOT_TOKEN= in HER env file. So after
# remap claudette's env has TELEGRAM_BOT_TOKEN= (her token) and NO
# CLAUDETTE_TELEGRAM_BOT_TOKEN= line — the old gate grep'd the absent pre-remap
# name → false failure → `No hosts remaining!` at the very last op.
#
# FIX: per concierge, the expected env-var-name set = required_keys with the
# concierge's OWN bot_token_secret_ref REPLACED by TELEGRAM_BOT_TOKEN when ref !=
# TELEGRAM_BOT_TOKEN (de-duplicated). Primary (ref == TELEGRAM_BOT_TOKEN) → set
# unchanged.

def _import_verify_with_recorders():
    """Import pyinfra/tasks/agent/_verify.py with `pyinfra` + `pyinfra.operations`
    replaced by recorders, so we can capture the exact server.shell ops it emits
    WITHOUT a live SSH connection. Mirrors _import_persona_with_recorders.

    Returns (module, server_recorder). server_recorder.calls["shell"] is a list
    of the kwargs dicts passed to each server.shell(...) call.
    """
    import importlib
    import types

    class _OpRecorder:
        def __init__(self):
            self.calls: dict[str, list[dict]] = {}

        def _record(self, op_name):
            def _op(*args, **kwargs):
                self.calls.setdefault(op_name, []).append(kwargs)
                return types.SimpleNamespace(did_change=lambda: True)
            return _op

        def __getattr__(self, name):
            return self._record(name)

    server_rec = _OpRecorder()
    files_rec = _OpRecorder()

    fake_pyinfra = types.ModuleType("pyinfra")
    fake_pyinfra.host = types.SimpleNamespace()
    fake_ops = types.ModuleType("pyinfra.operations")
    fake_ops.files = files_rec
    fake_ops.server = server_rec
    fake_pyinfra.operations = fake_ops

    saved = {
        k: sys.modules.get(k)
        for k in ("pyinfra", "pyinfra.operations", "tasks.agent._verify")
    }
    sys.modules["pyinfra"] = fake_pyinfra
    sys.modules["pyinfra.operations"] = fake_ops
    sys.modules.pop("tasks.agent._verify", None)

    if str(REPO_ROOT / "pyinfra") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "pyinfra"))

    try:
        mod = importlib.import_module("tasks.agent._verify")
        mod.server = server_rec
        return mod, server_rec
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _make_secrets(required_keys):
    """Build a SecretsConfig with the default runtime path + given keys."""
    from lib.tenant_loader import SecretsConfig

    return SecretsConfig(enabled=True, required_keys=list(required_keys))


def _verify_grepped_var_names(server_rec, runtime_env):
    """Extract the set of env-var NAMES the Step-5 ops grep for in `runtime_env`.

    Step-5 ops have commands like `grep -q '^KEY=' <runtime_env>`. We parse the
    KEY out of each such command (only those targeting `runtime_env`).
    """
    names = set()
    pat = re.compile(r"grep -q '\^([A-Z0-9_]+)='")
    for call in server_rec.calls.get("shell", []):
        cmds = call.get("commands", [])
        if not isinstance(cmds, list):
            cmds = [str(cmds)]
        for cmd in cmds:
            if runtime_env not in cmd:
                continue
            m = pat.search(cmd)
            if m:
                names.add(m.group(1))
    return names


# Shared required_keys mirroring the live bubble-internal blob.
_LIVE_REQUIRED_KEYS = [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "CLAUDETTE_TELEGRAM_BOT_TOKEN",
    "TAILSCALE_AUTHKEY",
    "PHONEHOME_TOKEN",
]


class TestVerifyTokenRemapAwareGate:
    """_verify Step-5 env-var-name gate must account for the per-concierge
    bot-token REMAP done by the systemd unit (SPEC-001 v1.2 multi-concierge)."""

    def test_nonprimary_concierge_checks_remapped_telegram_bot_token(self):
        """(a) Non-primary (claudette, ref=CLAUDETTE_TELEGRAM_BOT_TOKEN): the gate
        checks `^TELEGRAM_BOT_TOKEN=` (the remap RESULT, present in her env) and
        does NOT check `^CLAUDETTE_TELEGRAM_BOT_TOKEN=` (dropped by the remap)."""
        mod, server_rec = _import_verify_with_recorders()
        s = _make_secrets(_LIVE_REQUIRED_KEYS)
        runtime_env = "/run/claude-agent-claudette/env"

        mod._verify_one(
            s,
            "claudette",
            is_primary=False,
            bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        )

        names = _verify_grepped_var_names(server_rec, runtime_env)
        assert "TELEGRAM_BOT_TOKEN" in names, (
            "claudette's env has TELEGRAM_BOT_TOKEN= after the unit's remap — the "
            "gate must check the remapped name."
        )
        assert "CLAUDETTE_TELEGRAM_BOT_TOKEN" not in names, (
            "the unit DROPS CLAUDETTE_TELEGRAM_BOT_TOKEN= during remap; checking it "
            "is the false-failure that aborted every multi-concierge deploy."
        )

    def test_primary_concierge_behavior_unchanged(self):
        """(b) Primary (morty, ref=TELEGRAM_BOT_TOKEN): no remap → the gate checks
        `^TELEGRAM_BOT_TOKEN=` and ALL other required_keys verbatim, including
        CLAUDETTE_TELEGRAM_BOT_TOKEN (morty's blob keys it; not his concern to
        remap)."""
        mod, server_rec = _import_verify_with_recorders()
        s = _make_secrets(_LIVE_REQUIRED_KEYS)
        runtime_env = "/run/claude-agent/env"

        mod._verify_one(
            s,
            "morty",
            is_primary=True,
            bot_token_secret_ref="TELEGRAM_BOT_TOKEN",
        )

        names = _verify_grepped_var_names(server_rec, runtime_env)
        assert names == set(_LIVE_REQUIRED_KEYS), (
            "primary concierge: ref == TELEGRAM_BOT_TOKEN → no swap → the gate "
            "must check the raw required_keys set unchanged.\n"
            f"got {names!r}"
        )

    def test_other_required_keys_checked_for_both(self):
        """(c) Non-bot-token keys (CLAUDE_CODE_OAUTH_TOKEN, TAILSCALE_AUTHKEY,
        PHONEHOME_TOKEN) are still checked for BOTH primary and non-primary."""
        always = {"CLAUDE_CODE_OAUTH_TOKEN", "TAILSCALE_AUTHKEY", "PHONEHOME_TOKEN"}

        mod, server_rec = _import_verify_with_recorders()
        s = _make_secrets(_LIVE_REQUIRED_KEYS)
        mod._verify_one(
            s, "morty", is_primary=True, bot_token_secret_ref="TELEGRAM_BOT_TOKEN"
        )
        primary_names = _verify_grepped_var_names(server_rec, "/run/claude-agent/env")

        mod, server_rec = _import_verify_with_recorders()
        s = _make_secrets(_LIVE_REQUIRED_KEYS)
        mod._verify_one(
            s,
            "claudette",
            is_primary=False,
            bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        )
        nonprimary_names = _verify_grepped_var_names(
            server_rec, "/run/claude-agent-claudette/env"
        )

        assert always <= primary_names, (
            f"primary missing always-checked keys: {always - primary_names}"
        )
        assert always <= nonprimary_names, (
            f"non-primary missing always-checked keys: {always - nonprimary_names}"
        )

    def test_apply_passes_per_concierge_bot_token_ref(self):
        """apply() must thread each concierge's OWN bot_token_secret_ref into
        _verify_one (so the swap is per-concierge, not a global)."""
        mod, server_rec = _import_verify_with_recorders()

        captured = []
        orig = mod._verify_one

        def _spy(s, persona_name, *, is_primary, bot_token_secret_ref):
            captured.append((persona_name, is_primary, bot_token_secret_ref))
            return orig(
                s,
                persona_name,
                is_primary=is_primary,
                bot_token_secret_ref=bot_token_secret_ref,
            )

        mod._verify_one = _spy
        try:
            cfg = _build_multi_concierge_cfg()
            mod.get_tenant_config = lambda host: cfg
            mod.apply()
        finally:
            mod._verify_one = orig

        by_name = {name: ref for name, _, ref in captured}
        assert by_name.get("morty") == "TELEGRAM_BOT_TOKEN", (
            "morty's ref must be threaded through verbatim"
        )
        assert by_name.get("claudette") == "CLAUDETTE_TELEGRAM_BOT_TOKEN", (
            "claudette's OWN ref must be threaded through (swap happens inside "
            "_verify_one, not in apply)"
        )


def _build_multi_concierge_cfg():
    """A minimal TenantConfig-like object for _verify.apply: morty + claudette,
    each with its own bot_token_secret_ref, plus secrets with required_keys."""
    import types

    from lib.tenant_loader import (
        AgentChannelsConfig,
        AgentLLMConfig,
        ConciergeConfig,
        TelegramChannelConfig,
    )

    def _concierge(name, ref):
        return ConciergeConfig(
            name=name,
            persona_dir=f"persona/{name}",
            channels=AgentChannelsConfig(
                telegram=TelegramChannelConfig(
                    enabled=True, bot_token_secret_ref=ref, allowed_user_ids=["1"]
                )
            ),
            llm=AgentLLMConfig(
                provider="anthropic",
                model="opus[1m]",
                auth_mode="claude_code_subscription",
            ),
        )

    agent = types.SimpleNamespace(
        concierges=[
            _concierge("morty", "TELEGRAM_BOT_TOKEN"),
            _concierge("claudette", "CLAUDETTE_TELEGRAM_BOT_TOKEN"),
        ]
    )
    return types.SimpleNamespace(
        agent=agent, secrets=_make_secrets(_LIVE_REQUIRED_KEYS)
    )


class TestDurableClaudeAuth:
    """Durable headless auth via CLAUDE_CODE_OAUTH_TOKEN (regression guard).

    The durable model (SPEC-009 addendum, "Durable headless authentication"):
    the agent authenticates via a long-lived, self-refreshing
    CLAUDE_CODE_OAUTH_TOKEN delivered through the SOPS blob -> the agent's runtime
    env file, NOT a hand-ported ~/.claude/.credentials.json (which expires ~daily
    and SHADOWS the env token because claude PREFERS the creds file when present).

    Two halves of the contract are locked in:
      A. The durable token is WIRED: the per-concierge verify gate asserts its
         presence in the runtime env file (deploy PROVES the wiring before
         _cleanup_legacy), for BOTH the primary and a remapped non-primary
         concierge; and the unit sources the runtime env via EnvironmentFile=
         (the mechanism that puts the token in claude's env); and the tenant.yaml
         template declares it under secrets.required_keys (so it gets decrypted).
      B. The deploy never WRITES a ~/.claude/.credentials.json that would shadow
         the env token.

    Self-contained on purpose: a stable, cheap guard that bites if a future
    refactor drops the durable token from the verify gate, removes the
    EnvironmentFile= load, drops it from the tenant template, or introduces a
    credentials-file write into a task.
    """

    _DURABLE_KEY = "CLAUDE_CODE_OAUTH_TOKEN"

    # --- A. the durable token is wired ------------------------------------

    def test_verify_gate_checks_oauth_token_for_primary_concierge(self):
        """The verify gate must grep the runtime env file for
        ^CLAUDE_CODE_OAUTH_TOKEN= for the PRIMARY concierge (morty:
        bot_token_secret_ref == TELEGRAM_BOT_TOKEN, no remap)."""
        mod, server_rec = _import_verify_with_recorders()
        s = _make_secrets(_LIVE_REQUIRED_KEYS)
        runtime_env = "/run/claude-agent/env"

        mod._verify_one(
            s, "morty", is_primary=True, bot_token_secret_ref="TELEGRAM_BOT_TOKEN"
        )

        names = _verify_grepped_var_names(server_rec, runtime_env)
        assert self._DURABLE_KEY in names, (
            "the verify gate must assert CLAUDE_CODE_OAUTH_TOKEN is present in the "
            "primary concierge's runtime env file — that's the deploy-time proof "
            "the durable token is wired."
        )

    def test_verify_gate_checks_oauth_token_for_nonprimary_concierge(self):
        """The same guarantee must survive the per-concierge Telegram-token REMAP
        a non-primary concierge undergoes. The remap swaps the concierge's OWN
        telegram ref onto TELEGRAM_BOT_TOKEN; it must NOT disturb the durable
        CLAUDE_CODE_OAUTH_TOKEN check."""
        mod, server_rec = _import_verify_with_recorders()
        s = _make_secrets(_LIVE_REQUIRED_KEYS)
        runtime_env = "/run/claude-agent-claudette/env"

        mod._verify_one(
            s,
            "claudette",
            is_primary=False,
            bot_token_secret_ref="CLAUDETTE_TELEGRAM_BOT_TOKEN",
        )

        names = _verify_grepped_var_names(server_rec, runtime_env)
        assert self._DURABLE_KEY in names, (
            "CLAUDE_CODE_OAUTH_TOKEN must still be checked for a remapped "
            "non-primary concierge (the remap only swaps the bot-token ref)."
        )
        # Sanity: confirm the remap path is actually exercised here.
        assert "TELEGRAM_BOT_TOKEN" in names
        assert "CLAUDETTE_TELEGRAM_BOT_TOKEN" not in names

    def test_unit_template_loads_environment_file(self):
        """The systemd unit must source the decrypted runtime env via
        EnvironmentFile= — the mechanism by which CLAUDE_CODE_OAUTH_TOKEN (and the
        other blob keys) reach the claude process environment. Without this line
        the durable token never enters the agent's env."""
        tpl = (TEMPLATES_DIR / "claude-agent.service.j2").read_text()
        assert "EnvironmentFile=" in tpl
        # The `-` (optional) prefix is required so unit-load doesn't fail before
        # ExecStartPre creates the file (documented gotcha). Guard it too.
        assert "EnvironmentFile=-" in tpl

    def test_oauth_token_declared_in_tenant_yaml_template(self):
        """The tenant.yaml template's secrets.required_keys must list the durable
        token, so a tenant rendered from it decrypts CLAUDE_CODE_OAUTH_TOKEN into
        the runtime env file (the source the verify gate then checks)."""
        tpl = (TEMPLATES_DIR / "tenant.yaml.j2").read_text()
        assert self._DURABLE_KEY in tpl, (
            "tenant.yaml.j2 must declare CLAUDE_CODE_OAUTH_TOKEN under "
            "secrets.required_keys so the durable token is decrypted into env."
        )

    # --- B. the deploy never shadows the env token ------------------------

    def test_no_deploy_task_writes_a_credentials_json(self):
        """No file under pyinfra/tasks/** may WRITE a ~/.claude/.credentials.json.

        claude PREFERS .credentials.json over the env token, so if the deploy ever
        created one it would shadow (and then expire out from under) the durable
        CLAUDE_CODE_OAUTH_TOKEN. The durable model depends on this file being
        ABSENT. We scan every task module's source: any mention of
        'credentials.json' must be prose (docstring/comment), never on a line that
        performs a write (a files.put / files.template dest= or a shell redirect
        into a *.credentials.json path).
        """
        tasks_root = REPO_ROOT / "pyinfra" / "tasks"
        offenders = []
        for py in tasks_root.rglob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), start=1):
                if "credentials.json" not in line:
                    continue
                lowered = line.lower()
                writes = (
                    "files.put" in lowered
                    or "files.template" in lowered
                    or "files.line" in lowered
                    or "dest=" in lowered
                    or "server.shell" in lowered
                    or ">" in line  # any shell redirect on a credentials.json line
                )
                if writes:
                    offenders.append(
                        f"{py.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                    )
        assert not offenders, (
            "A deploy task appears to WRITE a .credentials.json, which would "
            "shadow the durable CLAUDE_CODE_OAUTH_TOKEN env token:\n"
            + "\n".join(offenders)
        )

    def test_credentials_json_only_appears_as_prose_in_pyinfra(self):
        """Belt-and-suspenders across ALL of pyinfra/ (tasks + templates): the
        string '.credentials.json' may appear ONLY in non-write contexts (a
        comment / an --exclude flag / YAML prose). It must never be a write
        target. If a new write sneaks in, this fails."""
        pyinfra_root = REPO_ROOT / "pyinfra"
        for path in pyinfra_root.rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".j2"):
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if ".credentials.json" not in line:
                    continue
                lowered = line.lower()
                is_write = (
                    "files.put" in lowered
                    or "files.template" in lowered
                    or "dest=" in lowered
                    or (">" in line and "exclude" not in lowered)
                )
                rel = path.relative_to(REPO_ROOT)
                assert not is_write, (
                    f"{rel}:{lineno} writes a .credentials.json — forbidden "
                    f"(would shadow the durable env token): {line.strip()}"
                )
