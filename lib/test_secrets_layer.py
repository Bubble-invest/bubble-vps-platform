"""Static tests for the secrets layer pyinfra modules.

These tests do NOT run pyinfra — they grep the source of the task modules
to enforce architectural invariants:

    1. The SPEC-008 hard rule: every `sops --decrypt` invocation MUST be
       followed by a redirection or filter that drops/masks the plaintext
       (one of `> /dev/null`, `| grep -q`, `| wc`). This prevents future
       edits from accidentally leaking decrypted secrets into pyinfra's
       stdout/stderr (and thus into operator session JSONL transcripts).
       The lesson came from a real incident on 2026-05-08.

    2. The module uses the SPEC-003 helper (`lib.host_helpers.get_tenant_config`)
       instead of parsing tenant.yaml directly. Direct YAML parsing in pyinfra
       tasks would bypass the host-data exposure policy and likely leak PII
       into pyinfra debug output.

Run with: python3.12 -m pytest lib/test_secrets_layer.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SOPS_DEPLOY_PATH = REPO_ROOT / "pyinfra" / "tasks" / "secrets" / "_sops_deploy.py"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _read_module_source() -> str:
    """Read the _sops_deploy module source. Fails the test if the file is
    missing — that's a structural regression the test is designed to catch."""
    assert SOPS_DEPLOY_PATH.is_file(), (
        f"_sops_deploy.py not found at {SOPS_DEPLOY_PATH}. "
        f"This module is the second-half of Phase D (SPEC-008); without it "
        f"the secrets layer cannot ship encrypted blobs to the box."
    )
    return SOPS_DEPLOY_PATH.read_text(encoding="utf-8")


def _strip_comments_and_strings(source: str) -> str:
    """Remove Python comments and docstrings before scanning for sops calls.

    Without this, the test would flag the module's own docstring (which
    legitimately mentions `sops --decrypt FILE` as a forbidden pattern in
    its safety commentary) as a violation. We want to scan only EXECUTABLE
    code — the strings passed to server.shell commands list.

    Implementation: strip line comments (#...) and triple-quoted strings.
    Single/double-quoted single-line strings are KEPT because that's where
    the actual shell commands live (passed as Python string literals to
    `commands=[...]`).
    """
    # Remove triple-quoted strings (docstrings + multi-line literals).
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
    # Remove # line comments.
    no_comments = re.sub(r"#[^\n]*", "", no_triple)
    return no_comments


def _join_adjacent_string_literals(source: str) -> str:
    """Collapse Python's implicit string-literal concatenation in `source`.

    Pyinfra task modules often build long shell commands by writing several
    adjacent Python string literals on consecutive lines, e.g.:

        commands=[
            f"SOPS_AGE_KEY_FILE={path} sops --decrypt "
            f"{file} > /dev/null"
        ]

    At parse time Python concatenates the two literals into one string. Our
    static scan needs to see the same single command, otherwise it fails to
    notice that `> /dev/null` is on the second physical line.

    This function deletes any `"<whitespace>"` or `'<whitespace>'` boundary
    between consecutive string literals — collapsing the two literals into
    one. It's a heuristic (we don't tokenize Python properly), but it's
    correct enough for the constrained shapes our task modules use.
    """
    # Collapse: "<close-quote> <whitespace incl. newlines and prefixes> <open-quote>"
    # where prefixes = optional f/r/b before the opening quote on the next literal.
    pattern = re.compile(r'"\s*(?:[fFrRbB]{0,2})"', re.DOTALL)
    collapsed = pattern.sub("", source)
    pattern_single = re.compile(r"'\s*(?:[fFrRbB]{0,2})'", re.DOTALL)
    collapsed = pattern_single.sub("", collapsed)
    return collapsed


# ─── Tests ──────────────────────────────────────────────────────────────────

class TestSopsDeployMaskingRule:
    """SPEC-008 hard rule: no `sops --decrypt` output may reach stdout/stderr."""

    def test_module_exists(self):
        assert SOPS_DEPLOY_PATH.is_file(), (
            "pyinfra/tasks/secrets/_sops_deploy.py is missing"
        )

    def test_no_unredirected_decrypt_in_source(self):
        """Every `sops --decrypt ...` invocation in executable code MUST be
        followed by a redirection or filter that drops/masks plaintext.

        Allowed continuations (case-sensitive, on the same logical line):
            ` > /dev/null`     — exit code only
            ` | grep -q`       — existence check, no value visible
            ` | wc`            — line count signature, no values

        Forbidden:
            `sops --decrypt FILE` alone, or piped to head/cat/etc.
        """
        source = _join_adjacent_string_literals(
            _strip_comments_and_strings(_read_module_source())
        )

        # Find every position where `sops --decrypt` (or {_SOPS_BIN} --decrypt
        # when constructed via f-string with the constant we use) appears in
        # executable code.
        # We accept multiple ways the binary path can be referenced:
        #   - literal "/usr/local/bin/sops --decrypt"
        #   - f-string substitution "{_SOPS_BIN} --decrypt"
        # Both should be followed by the same masking pattern.
        decrypt_re = re.compile(
            r"(?:/usr/local/bin/sops|\{_SOPS_BIN\}|\bsops)\s+--decrypt\b"
        )
        violations: list[str] = []
        for m in decrypt_re.finditer(source):
            # Look at the next ~120 chars (same logical Python string literal,
            # which contains the full shell command) for the masking pattern.
            tail = source[m.end():m.end() + 200]
            # The shell command lives inside a Python string literal, so it
            # ends at the closing quote of that literal. Grab up to the next
            # quote boundary OR the next newline (whichever comes first).
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
            )
            if not allowed:
                # Reconstruct context for an actionable failure message.
                line_no = source[: m.start()].count("\n") + 1
                snippet = source[m.start(): m.start() + 120].replace("\n", "\\n")
                violations.append(
                    f"line ~{line_no}: unredirected `sops --decrypt` near: {snippet!r}"
                )

        assert not violations, (
            "SPEC-008 hard rule violated: every `sops --decrypt` invocation "
            "MUST be followed by `> /dev/null`, `| grep -q`, or `| wc` to "
            "prevent plaintext from reaching pyinfra stdout/stderr (and thus "
            "operator session transcripts).\n\n"
            "Violations found in pyinfra/tasks/secrets/_sops_deploy.py:\n  "
            + "\n  ".join(violations)
        )

    def test_no_decrypt_to_disk(self):
        """`sops --decrypt FILE > /tmp/...` (or any path-redirect except
        /dev/null) persists plaintext to disk. SPEC-008 forbids this entirely
        — even briefly. The systemd ExecStartPre at Step 4 will use a tmpfs
        path, but never this module."""
        source = _join_adjacent_string_literals(
            _strip_comments_and_strings(_read_module_source())
        )
        # Match `sops --decrypt ... > /<path>` where /<path> is NOT /dev/null.
        bad_redirect_re = re.compile(
            r"sops\s+--decrypt[^\"'\n]*?>\s*(/(?!dev/null)\S+)"
        )
        matches = bad_redirect_re.findall(source)
        assert not matches, (
            "Found `sops --decrypt ... > <path>` redirecting plaintext to "
            f"disk: {matches}. Only `> /dev/null` is permitted."
        )


class TestSopsDeployUsesHostHelper:
    """Architectural invariant: tasks read tenant config via the helper, not
    by parsing YAML directly. This keeps the SPEC-003 host-data exposure
    policy enforceable in one place."""

    def test_module_imports_get_tenant_config(self):
        source = _read_module_source()
        # Accept either form:
        #   from lib.host_helpers import get_tenant_config
        #   from lib.host_helpers import (get_tenant_config, ...)
        assert re.search(
            r"from\s+lib\.host_helpers\s+import[^\n]*\bget_tenant_config\b",
            source,
        ), (
            "_sops_deploy.py must import get_tenant_config from "
            "lib.host_helpers — direct YAML parsing in pyinfra tasks bypasses "
            "the SPEC-003 host-data exposure policy."
        )

    def test_module_does_not_import_yaml(self):
        """No direct yaml import — config access must go through the helper."""
        source = _read_module_source()
        assert not re.search(r"^\s*import\s+yaml\b", source, re.MULTILINE), (
            "_sops_deploy.py imports yaml directly. It should call "
            "get_tenant_config(host) instead — see SPEC-003."
        )


class TestSopsDeployOrchestration:
    """Verify deploy.py wires _sops_deploy into the orchestration chain."""

    def test_deploy_imports_sops_deploy(self):
        deploy_path = REPO_ROOT / "pyinfra" / "tasks" / "secrets" / "deploy.py"
        source = deploy_path.read_text(encoding="utf-8")
        assert "_sops_deploy" in source, (
            "tasks/secrets/deploy.py does not import _sops_deploy — the "
            "second-half of Phase D would never run."
        )

    def test_deploy_calls_sops_deploy_apply(self):
        deploy_path = REPO_ROOT / "pyinfra" / "tasks" / "secrets" / "deploy.py"
        source = deploy_path.read_text(encoding="utf-8")
        assert re.search(r"_sops_deploy\.apply\s*\(", source), (
            "tasks/secrets/deploy.py imports _sops_deploy but never calls "
            "_sops_deploy.apply() — the encrypted blob would never ship."
        )


class TestSecretsRestartOnChange:
    """SPEC-012 — when secrets.sops.env content changes, the agent service
    must restart so its decrypted /run/claude-agent/env refreshes. The
    pattern mirrors agent/_settings.py: capture the upload op, gate a
    `systemctl restart` on `upload_op.did_change`, tolerate cold-deploy
    ordering with a `systemctl list-unit-files` guard."""

    def test_sops_deploy_module_has_restart_on_change(self):
        """Static check: the module must contain a server.shell op that
        restarts the agent service ONLY when the upload op changed state.

        Required ingredients:
          - A server.shell whose `name=` mentions "restart"
          - `_if=upload_op.did_change` gating clause
          - `_sudo=True` argument (systemctl needs root)
          - A `list-unit-files` guard so cold-deploy (no service yet) is a
            no-op rather than a hard failure.
        """
        source = _read_module_source()

        # Locate the restart server.shell call: a server.shell whose `name=`
        # argument literally contains "restart". We grep against the joined
        # source so multiline string-literal name= still matches.
        joined = _join_adjacent_string_literals(source)

        assert re.search(
            r"server\.shell\s*\([^)]*name\s*=\s*[fFrR]?[\"'][^\"']*restart[^\"']*[\"']",
            joined,
            re.DOTALL,
        ), (
            "SPEC-012: _sops_deploy.py must include a server.shell op whose "
            "name= mentions 'restart' (gating the agent service restart on "
            "upload_op.did_change). Pattern not found."
        )

        assert re.search(
            r"_if\s*=\s*upload_op\.did_change",
            joined,
        ), (
            "SPEC-012: the restart op must be gated with "
            "`_if=upload_op.did_change` so it only fires when the encrypted "
            "secrets file actually changed. Without this, every deploy "
            "would restart the agent — breaking idempotency."
        )

        assert re.search(
            r"_sudo\s*=\s*True",
            joined,
        ), (
            "SPEC-012: the restart op must pass `_sudo=True` — systemctl "
            "restart requires root, and the tenant ssh user is non-root."
        )

        assert "list-unit-files" in joined, (
            "SPEC-012: the restart command must guard with "
            "`systemctl list-unit-files <service> >/dev/null 2>&1` so that "
            "cold-deploy (service not yet installed) is a no-op instead of "
            "a hard failure. Pattern not found."
        )

    def test_sops_deploy_restart_uses_get_tenant_config_for_persona_name(self):
        """The restart op must derive the service name from the tenant config —
        NOT hardcoded. SPEC-001 v1.2 (multi-concierge): the module now LOOPS over
        `cfg.agent.concierges` and restarts each `claude-agent-<name>.service`,
        so the per-concierge name comes from `concierge.name` (legacy single
        form: `cfg.agent.persona.name`). Either is tenant-portable.

        This proves the module is tenant-portable: a future tenant whose
        concierge is `ricky` will get `claude-agent-ricky.service` restarted,
        not `claude-agent-morty.service`."""
        source = _read_module_source()

        # Multi-concierge loop form: `for concierge in cfg.agent.concierges`
        # + `concierge.name`. Legacy form: `cfg.agent.persona.name`.
        has_loop = (
            "cfg.agent.concierges" in source and "concierge.name" in source
        )
        has_direct = "cfg.agent.persona.name" in source
        has_alias = bool(re.search(
            r"persona_name\s*=\s*cfg\.agent\.persona\.name",
            source,
        ))

        assert has_loop or has_direct or has_alias, (
            "SPEC-012/SPEC-001 v1.2: restart command must reference the tenant "
            "config — either loop over cfg.agent.concierges (multi-concierge) "
            "or cfg.agent.persona.name (legacy). Hardcoded persona names like "
            "'morty' or 'ricky' would break tenant portability."
        )

        # And the service name template must use a variable, not a literal.
        joined = _join_adjacent_string_literals(source)
        assert re.search(
            r"claude-agent-\{persona_name\}\.service",
            joined,
        ) or re.search(
            r"claude-agent-\{cfg\.agent\.persona\.name\}\.service",
            joined,
        ) or re.search(
            r"claude-agent-\{concierge\.name\}\.service",
            joined,
        ), (
            "SPEC-012: the systemd service name in the restart command must "
            "be templated as `claude-agent-{persona_name}.service`, "
            "`claude-agent-{cfg.agent.persona.name}.service`, or (multi-concierge) "
            "`claude-agent-{concierge.name}.service` so the right service(s) "
            "get restarted."
        )

    def test_sops_deploy_module_no_unredirected_decrypt_in_restart_op(self):
        """The new restart op must NOT itself invoke `sops --decrypt` without
        the SPEC-008 redirection (`> /dev/null`, `| grep -q`, `| wc`).

        This naturally passes today (the restart command only runs systemctl,
        no decryption) — but the static guard prevents a future regression
        where someone might add an extra "verify after restart" step that
        decrypts and accidentally lets plaintext leak.

        Implementation: re-run the module-wide masking scan from
        TestSopsDeployMaskingRule.test_no_unredirected_decrypt_in_source,
        which covers EVERY `sops --decrypt` in executable code, including
        any new ops like the restart server.shell.
        """
        source = _join_adjacent_string_literals(
            _strip_comments_and_strings(_read_module_source())
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
            )
            if not allowed:
                line_no = source[: m.start()].count("\n") + 1
                snippet = source[m.start(): m.start() + 120].replace("\n", "\\n")
                violations.append(
                    f"line ~{line_no}: unredirected `sops --decrypt` near: {snippet!r}"
                )

        assert not violations, (
            "SPEC-008 hard rule violated by an op in _sops_deploy.py "
            "(possibly the new SPEC-012 restart op or a future addition): "
            "every `sops --decrypt` invocation MUST be followed by "
            "`> /dev/null`, `| grep -q`, or `| wc`.\n\n"
            "Violations:\n  " + "\n  ".join(violations)
        )
