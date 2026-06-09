"""TDD for pyinfra/tasks/agent/_os_user.py — the per-dept user provisioner.

The provisioner is the OPT-IN half of the agent_os_user migration. It must:
  - be a NO-OP for the legacy `claude` user (emit ZERO ops), and
  - for a per-dept user, create a hardened SYSTEM user (nologin, no password,
    NOT in the sudo group) and chown the agent's dirs to it.

We exercise the module with a recorder that stands in for pyinfra.operations,
so no SSH connection is needed.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "pyinfra") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "pyinfra"))


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


def _import_os_user_with_recorders():
    files_rec = _OpRecorder()
    server_rec = _OpRecorder()

    fake_ops = types.ModuleType("pyinfra.operations")
    fake_ops.files = files_rec
    fake_ops.server = server_rec
    fake_pyinfra = types.ModuleType("pyinfra")
    fake_pyinfra.operations = fake_ops

    saved = {
        k: sys.modules.get(k)
        for k in ("pyinfra", "pyinfra.operations", "tasks.agent._os_user")
    }
    sys.modules["pyinfra"] = fake_pyinfra
    sys.modules["pyinfra.operations"] = fake_ops
    sys.modules.pop("tasks.agent._os_user", None)
    try:
        mod = importlib.import_module("tasks.agent._os_user")
        mod.files = files_rec
        mod.server = server_rec
    finally:
        pass
    return mod, files_rec, server_rec, saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


_PER_USER_KWARGS = dict(
    workdir="/srv/agents/morty",
    run_env_dir="/run/claude-agent-morty",
    channel_dir="/home/agent-morty/.claude/channels/telegram",
    home="/home/agent-morty",
)


def test_legacy_user_is_a_noop():
    mod, files_rec, server_rec, saved = _import_os_user_with_recorders()
    try:
        mod.apply_for_user("claude", **dict(
            workdir="/home/claude/agents/morty",
            run_env_dir="/run/claude-agent",
            channel_dir="/home/claude/.claude/channels/telegram",
            home="/home/claude",
        ))
        assert server_rec.calls == {}, "legacy user must NOT create an OS user"
        assert files_rec.calls == {}, "legacy user must NOT chown anything"
    finally:
        _restore(saved)


def test_per_user_creates_hardened_system_user():
    mod, files_rec, server_rec, saved = _import_os_user_with_recorders()
    try:
        mod.apply_for_user("agent-morty", **_PER_USER_KWARGS)
        assert "user" in server_rec.calls, "must call server.user for a per-dept user"
        user_call = server_rec.calls["user"][0]
        assert user_call["user"] == "agent-morty"
        assert user_call["system"] is True
        assert user_call["shell"] == "/usr/sbin/nologin"
        # Must NEVER be added to the sudo group.
        groups = user_call.get("groups", [])
        assert "sudo" not in groups, "per-dept user must NOT be in the sudo group"
    finally:
        _restore(saved)


def test_per_user_chowns_all_four_dirs_to_itself():
    mod, files_rec, server_rec, saved = _import_os_user_with_recorders()
    try:
        mod.apply_for_user("agent-morty", **_PER_USER_KWARGS)
        dir_calls = files_rec.calls.get("directory", [])
        chowned = {c["path"]: c for c in dir_calls}
        for expected in (
            "/srv/agents/morty",
            "/run/claude-agent-morty",
            "/home/agent-morty/.claude/channels/telegram",
            "/home/agent-morty/.claude",
        ):
            assert expected in chowned, f"per-user provision must chown {expected}"
            assert chowned[expected]["user"] == "agent-morty"
            assert chowned[expected]["group"] == "agent-morty"
    finally:
        _restore(saved)


def test_per_user_user_op_is_sudo():
    """Creating a system user + chowning root-owned dirs needs root."""
    mod, files_rec, server_rec, saved = _import_os_user_with_recorders()
    try:
        mod.apply_for_user("agent-morty", **_PER_USER_KWARGS)
        assert server_rec.calls["user"][0].get("_sudo") is True
        for c in files_rec.calls["directory"]:
            assert c.get("_sudo") is True
    finally:
        _restore(saved)
