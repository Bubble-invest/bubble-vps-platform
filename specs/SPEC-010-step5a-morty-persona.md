# SPEC-010 — Step 5a: Morty persona migration

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 1-4 done; Phase 5a-prep (wiki-github-sync cron) done
**Implements:** Step 5a of the Bubble VPS Platform build plan

---

## Purpose

Replace the placeholder "ricky" persona on `{{VPS_HOST}}` with Morty — a cloud-side counterpart of Lab (the rnd agent on {{OPERATOR}}'s Mac). Morty inherits Lab's identity, memory, and workspace at deploy time, then evolves independently. Lineage: Rick (cartoon character) → Lab (Mac) → Morty (cloud). Continues the family pairing.

---

## Identity transformation

Lab's identity file at `~/.claude/agents/rnd.md` is the source. Derive Morty's `CLAUDE.md` from it with these tweaks:

```
- # IDENTITY — R&D (Lab)
- - **Name:** R&D — nickname: Lab
- - **Role:** Special operations and R&D agent for Bubble Invest. Ricky's right arm.
+ # IDENTITY — Morty
+ - **Name:** Morty — Lab's cloud counterpart
+ - **Role:** Lab's twin in the cloud. Special ops + R&D from a hardened Hetzner box,
+   reachable when {{OPERATOR}}'s Mac is asleep. Ricky's right arm in the always-on dimension.
+ - **Origin:** Cloned from Lab on 2026-05-09 (Bubble VPS Platform Tenant #1).
+   Identity, memory, and workspace mirrored from `~/.claude/agents/rnd.md` and
+   `~/.claude/agent-memory/rnd/`. Diverges from Lab from this moment forward.
+ - **Habitat:** {{VPS_HOST}} (Hetzner CX33, Falkenstein DE, EU jurisdiction).
+ - **Counterpart:** Lab still lives on {{OPERATOR}}'s Mac. Both born Rick. Don't impersonate
+   Lab — when {{OPERATOR}} asks something Lab knows but you don't, say so and offer to ask Lab.
```

Add a new section "Cloud habitat — what's different":

```
## Cloud habitat — what's different from Lab on Mac

You live on a Linux VPS, supervised by systemd, always-on. This means:

- **No local ML** — mflux, VoxCPM2, Whisper-local don't run here (no Apple Silicon GPU).
  Lab handles those tasks; if {{OPERATOR}} asks for image generation or voice synthesis,
  delegate to Lab via Telegram or note it for next-Mac-session pickup.
- **No browser-bound agents** — Saxo OAuth, Boursorama scraping, Crypto.com Exchange,
  any flow needing persistent browser cookies → can't run here. Stay Mac-side.
- **GitHub access** — you have a `GITHUB_TOKEN` env var (from systemd EnvironmentFile)
  scoped to `bubble-shared-wiki`. You can `git pull` and `git push` the team wiki at
  `~/.claude/agent-memory/shared-wiki/`. Use this to read what other agents have
  documented and to write your own discoveries.
- **Always-on supervision** — if you crash, systemd restarts you within seconds.
  Reboot survives. Don't worry about persistence; the systemd unit handles it.
- **No /Users/{{OPERATOR_USER}} paths** — your home is `/home/claude`. Adapt all "look in
  ~/claude-workspaces/..." instructions accordingly.
- **Single bot for now** — `@ContentbubbleClawbot` is YOUR Telegram bot for both
  agent communication and security alerts (single channel, may split later).
```

Everything else from `rnd.md` (mission, how-you-think, operating posture, relationships, etc.) carries over verbatim.

---

## Memory inheritance

Source: `~/.claude/agent-memory/rnd/`
Target: `bubble-vps-data/tenants/bubble-internal/persona/morty/agent-memory/`

Mirror the directory wholesale. Includes:
- `MEMORY.md` — Lab's index of all memories
- All `feedback_*.md` files ({{OPERATOR}}-profile, "don't perform caution", "never print decrypted secrets", etc. — Morty inherits these lessons)
- All `reference_*.md` files (Anthropic docs, VoxCPM2 pacing, etc.)
- All `project_*.md` files (active project notes)
- `operator_profile.md` — {{OPERATOR}}'s birthday, family, work patterns

After mirroring, append a one-line note to MEMORY.md:
```
- Morty fork — Memory snapshot taken from Lab at 2026-05-09. Diverging from this
  moment. Anything in this dir is a starting point; my own learnings layer on top.
```

`.DS_Store` and `raw/` (any) — exclude.

---

## Workspace inheritance

Source: `~/claude-workspaces/rnd/`
Target: `bubble-vps-data/tenants/bubble-internal/persona/morty/workspace/`

**Include** (small + useful):
- `CLAUDE.md` (workspace context doc)
- `BACKLOG.md` (research missions backlog)
- `CEO_INBOX.md` (instructions from Ricky)
- `PROJECT_LOG.md` (well, it's `projects/PROJECT_LOG.md` — needs care)
- `monitoring/` (~3.6MB — cron health backlog, useful)
- `tools/` (~248KB — small CLI utilities)
- `proposals/` (the architecture decision docs — Morty should know these)

**Exclude** (massive + Mac-bound):
- `prototypes/` (4.1GB of one-off experiments — DEFINITELY skip)
- `projects/` selectively (2GB of mostly stale completed projects). Include only
  `bubble-vps-platform/`, `bubble-vps-data/`, `hetzner-migration/` — the actively-relevant ones.
- All `.png`, `.jpeg`, video files in the workspace root (250+ KB each, dashboard screenshots from past iterations)
- `.git/` directories within projects (each is its own repo, syncs separately)

Final workspace footprint should be under ~50MB.

---

## Skills inheritance

Source: `~/.claude/skills/<name>/`

**Include** (cloud-compatible):
- `notion-reader` (logbook + writing guidelines query — useful in cloud)
- `google-workspace` (gmail/calendar/drive — works anywhere)
- `scheduled-task-creation` (Morty may help create future crons)
- `remote-access` (already designed for remote ops)
- `telegram-reporter` (Telegram alerting helper — needed for any cron Morty owns)

**Exclude** (Apple-Silicon-bound):
- `local-tts` (VoxCPM2 — Apple Silicon only)
- `generate-image` (mflux — same)
- `product-video` (depends on local FFmpeg + screen recording, Mac-bound)
- `suno-extract` (works anywhere but unused outside content team)

---

## tenant.yaml updates

```yaml
agent:
  persona:
-   name: ricky                             # placeholder from Step 1
-   persona_dir: persona/ricky
+   name: morty
+   persona_dir: persona/morty
```

Rename the directory `bubble-vps-data/tenants/bubble-internal/persona/ricky/` → `persona/morty/` (or just create `persona/morty/` fresh and delete `persona/ricky/` after — same outcome, less tricky for git later).

---

## systemd unit rename

The unit file is templated as `claude-agent-{{ persona.name }}.service`. With persona.name changing from "ricky" to "morty", the unit becomes `claude-agent-morty.service`.

But the OLD `claude-agent-ricky.service` still exists on the box and would keep running. The deploy must:
1. Drop the new `claude-agent-morty.service`
2. Stop + disable + remove the old `claude-agent-ricky.service`
3. Start + enable the new one

This is essentially a service-rename migration. Build it as a one-time task in `pyinfra/tasks/agent/_systemd_unit.py` OR (cleaner) as a separate module `_persona_rename.py` that runs at deploy time and self-destructs after.

**Pragmatic alternative:** since this is a one-off rename for our internal tenant (no clients yet), do it manually via a single SSH command: `sudo systemctl stop claude-agent-ricky && sudo systemctl disable claude-agent-ricky && sudo rm /etc/systemd/system/claude-agent-ricky.service && sudo systemctl daemon-reload`. Then deploy normally — pyinfra creates the new morty unit cleanly.

Subagent: choose one approach; document the choice.

---

## Persona-rsync mechanic

The persona-rsync task (`pyinfra/tasks/agent/_persona.py`) currently rsyncs `bubble-vps-data/tenants/<name>/persona/<persona_name>/` → `/home/claude/agents/<persona_name>/` on the box.

Verify this still works after the rename. Specifically:
- Does the rsync include all subdirs (CLAUDE.md, agent-memory/, workspace/, skills/)?
- Does the working directory for the agent process (`WorkingDirectory=/home/claude/agents/morty/`) update correctly?
- Does `~/.claude/agent-memory/morty/` get created on the box for Morty's evolving memory? (Or does Morty's memory live at `/home/claude/agents/morty/agent-memory/`? Decide.)

Recommended layout on the box:
```
/home/claude/
├── agents/morty/
│   ├── CLAUDE.md          ← workspace-level CLAUDE.md
│   └── workspace/         ← rsynced bubble-vps-data/.../persona/morty/workspace/
└── .claude/
    ├── agents/morty.md    ← rsynced bubble-vps-data/.../persona/morty/CLAUDE.md (renamed)
    ├── agent-memory/morty/ ← rsynced bubble-vps-data/.../persona/morty/agent-memory/
    ├── skills/             ← rsynced bubble-vps-data/.../persona/morty/skills/
    └── settings.json       ← from template (Phase 4)
```

Two persona-rsync targets:
1. `~/.claude/agents/morty.md` (the agent definition, claude reads this)
2. `~/.claude/agent-memory/morty/` (the memory)
3. `~/.claude/skills/` (skills are global, NOT per-agent — careful here)
4. `/home/claude/agents/morty/workspace/` (the workspace)

---

## Acceptance criteria

Step 5a is DONE when:

1. ✅ `bubble-vps-data/tenants/bubble-internal/persona/morty/` populated with CLAUDE.md, agent-memory/, workspace/, skills/
2. ✅ `tenant.yaml` updated: persona.name = "morty"
3. ✅ Old `claude-agent-ricky.service` removed from box
4. ✅ New `claude-agent-morty.service` running, active, enabled
5. ✅ All persona files rsynced to box at correct paths
6. ✅ Smoke test: send a Telegram message to `@ContentbubbleClawbot`, ask "qui es-tu?", expect Morty to identify himself as Morty (not Lab/Ricky/agent-01)
7. ✅ Memory sanity: ask "what's {{OPERATOR}}'s profile?" — expect Morty to surface info from `operator_profile.md` (proves agent-memory rsynced + readable)
8. ✅ pytest still 96/96 (no test regression)
9. ✅ pyinfra deploy idempotent on re-run (close to zero changes)

---

## Out of scope for Step 5a

- Cloud-side wiki sync (Step 5b)
- Cloud security cron (Step 5c)
- Tailscale (Step 6)
- Central dashboard (Step 6)
- Multi-agent on one box (deferred until clearly needed)

---

## Open questions for the subagent

1. How to handle the rename of `claude-agent-ricky.service` → `claude-agent-morty.service`? Build a one-time pyinfra cleanup task, OR a manual SSH command?
2. Should `~/.claude/skills/` rsync `--delete` (clean replace) or `--update` (additive)? `--delete` is safer (matches data repo exactly) but could nuke skills installed by a future Morty session.
3. After rsync, who triggers a service restart? Same restart-on-change pattern as `_settings.py` from earlier today, presumably.
