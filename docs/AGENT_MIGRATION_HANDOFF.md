# Agent Migration — Apple Store Trip Handoff (2026-05-25 → 2026-05-28)

{{OPERATOR}}'s Mac going to Apple Store for 3 days. Three agents moved to keep the team alive:

## Migrations performed

| Agent | From | To | Mechanism |
|---|---|---|---|
| Maya (prospection) | Mac launchd | Morty VPS systemd (`ops-loop-maya.service`) | Done earlier this week — éclosion in progress |
| Claudette ({{OPERATOR_2}}'s assistant) | Mac launchd `com.claude.ricky-claudette` | Morty VPS systemd (`claude-agent-claudette.service`) | Done 2026-05-25 |
| Miranda (socials, needs browser) | Mac launchd `com.claude.ricky-socials` | {{OPERATOR_2}}'s Mac LaunchAgent (`com.claude.miranda.plist`) | Done 2026-05-25 — needs Chrome on a Mac |

## What got copied for each agent

For **Claudette → Morty** + **Miranda → {{OPERATOR_2}}'s Mac** (same recipe):

1. Identity file (`~/.claude/agents/<name>.md`)
2. Agent memory dir (`~/.claude/agent-memory/<name>/`)
3. Workspace (cloned from GitHub for Claudette; rsync'd for Miranda — 329 MB after excluding node_modules + venv + git objects)
4. JSONL conversation history (54 files Claudette / 84 files Miranda) with PATH RENAME so Claude Code recognizes them:
   - Mac source path: `~/.claude/projects/-Users-joris-claude-workspaces-Miranda-Socials/`
   - Target path: replace `-Users-joris-claude-workspaces-` with the host's `~/claude-workspaces/` rewritten as `-Users-<user>-claude-workspaces-` (Mac) or `-home-claude-agents-` (Linux)
5. Telegram channel config (`~/.claude/channels/telegram-<name>/{access.json, .env}`)
6. Telegram MCP plugin (`~/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6/`) + entry in `installed_plugins.json` + flag in `settings.json::enabledPlugins`
7. Trust-dialog acceptance in `~/.claude.json::projects.<cwd>.hasTrustDialogAccepted = true`
8. Systemd service unit (Morty) OR LaunchAgent plist (Mac) configured with `KeepAlive`/`Restart=on-failure`
9. Mac-side launchd `unload`'d on {{OPERATOR}}'s Mac to prevent duplicate Telegram polling

## Bot routing (unchanged from Mac days — same tokens reach same bots)

- Claudette: `@clawd_jadouBot` (now polled from Morty)
- Miranda: `@rickySocialsbot` (now polled from {{OPERATOR_2}} Mac)
- Maya: `@bubbleopsmaya_bot` (Morty)

## Reverting after the trip

For each agent on {{OPERATOR}}'s Mac:
```bash
launchctl load ~/Library/LaunchAgents/com.claude.ricky-claudette.plist
launchctl load ~/Library/LaunchAgents/com.claude.ricky-socials.plist
```

THEN immediately:
- Morty: `sudo systemctl stop claude-agent-claudette.service && sudo systemctl disable claude-agent-claudette.service`
- {{OPERATOR_2}} Mac: `launchctl unload ~/Library/LaunchAgents/com.claude.miranda.plist`

AND rsync JSONL conv history back to {{OPERATOR}} Mac (path-renamed back). The migration is symmetric.

## Things that may need attention during the trip

- **Maya éclosion**: Step 3 (Layers/PROMPT.md) — only L1 written, L2/L3/L4 pending. Morty can continue per the pattern committed in `bubble-rnd-workspace/projects/bubble-ops-loop/...`.
- **Claudette**: no scheduled tasks moved over — her crons were Mac-only (LaunchAgent-based). If a cron needs to fire during the trip, {{OPERATOR}} needs to either delegate Morty to handle it manually or let it slide for 3 days.
- **Miranda**: same — her LinkedIn/Twitter publishing crons were Mac-only LaunchAgents. If a scheduled post needs to fire, manual trigger via Telegram.

The agents themselves remain **conversationally available** — {{OPERATOR_2}} can chat with Claudette as normal, you can chat with both Maya and Miranda. Just the *scheduled* automated work is paused (we can resume it on return).

---

## Update 2026-05-25 12:25 UTC — Rick/Lab substrate bridge

Per {{OPERATOR}} msg 3198: long-term design is **two distinct personalities** (Lab on Mac, Morty on VPS) with **shared knowledge substrate**. Same memory + same conversation history visible to both, but separate identity files + tools + bots.

Synced to Morty:
- `~/.claude/agents/rnd.md` (Lab's identity, 167 lines) → readable on Morty as `/home/claude/.claude/agents/rnd.md`
- `~/.claude/agent-memory/rnd/` (Lab's memory pins, 67 files, 348 KB) → readable on Morty as `/home/claude/.claude/agent-memory/rnd/`
- Current `Rick_RnD` jsonl history (24 jsonl, ~99 MB) → `/home/claude/.claude/projects/-home-claude-agents-morty-workspace-projects-bubble-rnd-workspace/`
- Legacy `rnd` jsonl history pre-rename (54 jsonl, ~109 MB) → `/home/claude/.claude/projects/-home-claude-agents-morty-workspace-archive-rnd-old-path/`

Morty's `morty.md` identity file updated with a "Shared substrate" section that tells Morty:
- When to consult Lab's substrate (decisions/conversations from past Mac sessions, lessons-learned lookups)
- What stays distinct (identity, own memory dir, own tools, own bot)
- The reverse direction (Mac → Morty rsync deltas when Mac is back online)

Symmetric reverse on return: rsync Morty's notable session updates back to Mac so substrate stays current.
