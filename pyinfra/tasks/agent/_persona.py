"""Sync persona files from the data repo to the box (SPEC-007 + SPEC-010).

The persona directory in the data repo (e.g.
`bubble-vps-data/tenants/bubble-internal/persona/morty/`) holds FOUR distinct
sub-trees that route to different on-box locations:

    persona/<name>/CLAUDE.md       → ~/.claude/agents/<name>.md
        (the agent definition — claude reads this)
    persona/<name>/agent-memory/   → ~/.claude/agent-memory/<name>/
        (Morty's persistent memory — files Lab-style with frontmatter)
    persona/<name>/workspace/      → /home/claude/agents/<name>/workspace/
        (the agent's working directory tree — workspace-level CLAUDE.md,
         project notes, monitoring/, tools/, etc.)
        NOTE (SPEC-001 v1.3): this SYNCED workspace/ tree applies ONLY to
        concierges WITHOUT a `workspace_repo`. A GIT-BACKED concierge
        (workspace_repo set, e.g. claudette) instead has its workdir
        /home/claude/agents/<name> CLONED from its own git repo (clone-if-
        absent; no destructive sync). See the workspace branch in _apply_one.
    persona/<name>/skills/         → ~/.claude/skills/
        (skills are GLOBAL on the box — not per-agent — rsynced into place
         without --delete to avoid nuking other agents' future skills)

The systemd unit's `WorkingDirectory=/home/claude/agents/<persona_name>/`
points at the parent of the workspace/ tree. The agent's CWD therefore sees
its workspace/ subdir alongside any sibling state we may add later (logs,
caches, etc.).

Idempotency:
    pyinfra's `files.sync` mirrors a local dir tree to the remote with hash
    comparison per file — only re-uploads bytes that differ. Combined with
    `delete=True` it handles file removals cleanly. We use `delete=True` on
    agent-memory/ and workspace/ (data-repo is canonical for these), but
    NOT on skills/ (skills are global, other agents may add to that dir on
    the box). For the CLAUDE.md → agents/<name>.md rename we use files.put
    (single-file upload, hash-aware).
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server

from lib.host_helpers import get_tenant_config


def apply() -> None:
    """Sync persona files for EVERY concierge on the tenant (SPEC-001 v1.2).

    A box may host multiple concierges (morty + claudette). Each concierge's
    persona_dir is independent and routes to per-concierge on-box locations
    (workdir /home/claude/agents/<name>, agent-memory/<name>, agents/<name>.md).
    Skills are GLOBAL (shared across concierges) so they sync once per concierge
    additively (delete=False) — harmless if repeated.
    """
    cfg = get_tenant_config(host)
    for i, concierge in enumerate(cfg.agent.concierges):
        _apply_one(cfg, concierge, is_primary=(i == 0))


def _apply_one(cfg, concierge, *, is_primary: bool) -> None:
    persona_name = concierge.name
    # host.data.persona_dir points at the PRIMARY concierge's persona dir
    # (back-compat single-value inventory field). For additional concierges we
    # resolve their persona_dir relative to the tenant dir on the operator Mac.
    if is_primary:
        local_persona_dir = Path(host.data.persona_dir)
    else:
        local_persona_dir = (Path(cfg.tenant_dir) / concierge.persona_dir).resolve()

    # ─── 1. Workspace → /home/claude/agents/<name>/ ────────────────────────
    # Two models, chosen per-concierge (SPEC-001 v1.3):
    #
    #   GIT-BACKED (concierge.workspace_repo set, e.g. claudette): the workdir
    #   IS the concierge's OWN git repo. We CLONE it into the workdir directly
    #   (files at top level — NOT under a workspace/ subdir). The clone is
    #   idempotent (`test -d <dir>/.git || git clone`) and dangling-symlink
    #   aware (mirrors bubble-ops-loop/scripts/deploy-to-morty.sh). We do NOT
    #   auto-pull/reset on subsequent deploys — that could clobber the agent's
    #   uncommitted runtime work. Deploy only GUARANTEES the clone EXISTS;
    #   keeping it current is the agent's OWN `git pull` responsibility. And
    #   crucially: NO files.sync(delete=True) ever touches a git-backed workdir
    #   (that's the exact data-loss risk this branch avoids).
    #
    #   SYNCED (default, morty): the data repo holds a curated workspace/ tree
    #   that we mirror to /home/claude/agents/<name>/workspace/ with
    #   files.sync(delete=True) — the data repo is canonical for that workdir.
    if getattr(concierge, "workspace_repo", None):
        # Git-backed: clone-if-absent into the workdir. Guard explicitly on the
        # parent path so a DANGLING symlink (target gone) is a hard error rather
        # than a confusing `git clone` "File exists" crash, and a present dir/
        # symlink-to-dir with an existing .git is a no-op.
        workdir = f"/home/claude/agents/{persona_name}"
        repo_url = concierge.workspace_repo
        branch = getattr(concierge, "workspace_branch", "main") or "main"
        server.shell(
            name=(
                f"agent/persona: clone-if-absent workspace_repo for "
                f"{persona_name} into {workdir}"
            ),
            commands=[
                # Already a clone (test -d <dir>/.git) → no-op. Dangling symlink
                # → refuse (don't orphan the real data). Otherwise clone the
                # branch into the workdir directly (her repo files live at the
                # top level). The `test -d <dir>/.git ||` guard is what makes
                # this idempotent across re-deploys.
                f"if test -d {workdir}/.git; then "
                f":; "
                f"elif test -L {workdir} && ! test -e {workdir}; then "
                f"echo 'ERROR: {workdir} is a DANGLING symlink (target missing). "
                f"Fix the symlink target before deploying; refusing to clone "
                f"over it.' >&2; exit 1; "
                f"else "
                f"git clone --branch {branch} {repo_url} {workdir}; "
                f"fi"
            ],
            _sudo=True,
            _sudo_user="claude",
        )
    else:
        # Synced: ensure the workdir exists, then mirror the data-repo
        # workspace/ tree into it (data repo canonical, delete=True).
        files.directory(
            name=f"agent/persona: ensure /home/claude/agents/{persona_name} exists",
            path=f"/home/claude/agents/{persona_name}",
            user="claude",
            group="claude",
            mode="0755",
            present=True,
        )

        local_workspace = local_persona_dir / "workspace"
        if local_workspace.is_dir():
            files.directory(
                name=f"agent/persona: ensure /home/claude/agents/{persona_name}/workspace exists",
                path=f"/home/claude/agents/{persona_name}/workspace",
                user="claude",
                group="claude",
                mode="0755",
                present=True,
            )
            files.sync(
                name=(
                    f"agent/persona: rsync workspace/ to "
                    f"/home/claude/agents/{persona_name}/workspace"
                ),
                src=str(local_workspace),
                dest=f"/home/claude/agents/{persona_name}/workspace",
                user="claude",
                group="claude",
                delete=True,
            )

    # ─── 2. Agent definition → ~/.claude/agents/<name>.md ──────────────────
    # The persona-level CLAUDE.md is the AGENT DEFINITION (Lab's rnd.md
    # derivative). It must live at ~/.claude/agents/<name>.md so claude
    # picks it up.
    local_agent_def = local_persona_dir / "CLAUDE.md"
    if local_agent_def.is_file():
        files.directory(
            name="agent/persona: ensure /home/claude/.claude/agents exists",
            path="/home/claude/.claude/agents",
            user="claude",
            group="claude",
            mode="0755",
            present=True,
        )
        files.put(
            name=f"agent/persona: install agent definition at ~/.claude/agents/{persona_name}.md",
            src=str(local_agent_def),
            dest=f"/home/claude/.claude/agents/{persona_name}.md",
            user="claude",
            group="claude",
            mode="0644",
        )

    # ─── 3. Agent memory → ~/.claude/agent-memory/<name>/ ─────────────────
    # Mirror the data-repo memory snapshot to the box. Morty's own runtime
    # learnings will layer on top — once Morty starts editing files in
    # ~/.claude/agent-memory/morty/, we'd typically stop pushing here from
    # the data repo. For Step 5a (initial migration) this is a fresh seed.
    local_memory = local_persona_dir / "agent-memory"
    if local_memory.is_dir():
        files.directory(
            name="agent/persona: ensure /home/claude/.claude/agent-memory exists",
            path="/home/claude/.claude/agent-memory",
            user="claude",
            group="claude",
            mode="0755",
            present=True,
        )
        files.directory(
            name=f"agent/persona: ensure /home/claude/.claude/agent-memory/{persona_name} exists",
            path=f"/home/claude/.claude/agent-memory/{persona_name}",
            user="claude",
            group="claude",
            mode="0755",
            present=True,
        )
        files.sync(
            name=(
                f"agent/persona: rsync agent-memory/ to "
                f"/home/claude/.claude/agent-memory/{persona_name}"
            ),
            src=str(local_memory),
            dest=f"/home/claude/.claude/agent-memory/{persona_name}",
            user="claude",
            group="claude",
            delete=True,
        )

    # ─── 4. Skills → ~/.claude/skills/ (GLOBAL — no --delete) ─────────────
    # Skills are not per-agent; they're tools available to whichever agent
    # is running. We rsync without delete=True so other skills (potentially
    # installed by Morty himself, or added later by an operator) survive.
    # The trade-off: a skill removed from the data repo persists on-box
    # until manually cleaned. Acceptable for Step 5a — Morty's skills set
    # is small and curated.
    local_skills = local_persona_dir / "skills"
    if local_skills.is_dir():
        files.directory(
            name="agent/persona: ensure /home/claude/.claude/skills exists",
            path="/home/claude/.claude/skills",
            user="claude",
            group="claude",
            mode="0755",
            present=True,
        )
        files.sync(
            name="agent/persona: rsync skills/ to /home/claude/.claude/skills (additive)",
            src=str(local_skills),
            dest="/home/claude/.claude/skills",
            user="claude",
            group="claude",
            delete=False,
        )
