# Wave 2 — Bubble Cabinet (Docker container)

**Date:** 2026-06-08 · **Owner:** Rick (R&D) · **Status:** in_progress
**North Star:** `NORTH-STAR-BUBBLE-CABINET.md` — READ THIS FIRST.

## Sprint 1 — Dockerfile + docker-compose + install.sh

### W2.1 — Project scaffold
- Create `bubble-cabinet/` directory in the monorepo
- `.env.template` with all required vars
- `docker-compose.yml` with volumes and network isolation
- `scripts/install.sh` — one-shot setup script

### W2.2 — Dockerfile
- Ubuntu 24.04 base
- Install: git, curl, sops, age, python3.12, restic, jq, bun, claude-code
- Non-root `claude` user with minimal sudoers
- ENTRYPOINT: claude --channels for concierge

### W2.3 — SOPS+age key generation
- Install script generates age keypair if not present
- Stores in cabinet-age volume (mode 400)
- Generates restic passphrase

### W2.4 — Claude Code setup
- Install Claude Code via bun
- Configure for concierge persona
- Wire telegram plugin

### W2.5 — Verify
- docker compose build succeeds
- docker compose up → container healthy
- TDD: test scripts validate Dockerfile and compose structure

## Sprint 2 — Mode local-git

### W2.6 — BUBBLE_GIT_PROVIDER=local-bare
- bootstrap-dept.sh detects local-bare mode
- Creates bare repos in /srv/git-local/
- git-guard skips token mint for file:// remotes
- Tests: full bootstrap→activate without GitHub

## Sprint 3 — Client docs

### W2.7 — README-INSTALL.md (French)
### W2.8 — README-UPGRADE.md
### W2.9 — README-DISASTER.md

## Notion-agnostic constraint (all sprints)
- No Notion hardcoding — the framework works without it
- LOGBOOK_AGENT_ID and NOTION_API_KEY are optional in .env.template
