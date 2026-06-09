# SPEC-021 — Canonical per-department agent setup

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-31
**Depends on:** SPEC-007 (agent install), SPEC-009 (Claude Code subscription auth), SPEC-013 (Telegram recovery watchdog)
**Extends / supersedes-in-part:** [SPEC-013](SPEC-013-telegram-recovery-watchdog.md) (watchdog detection + recovery logic), [SPEC-009](SPEC-009-step4-addendum-claude-code-subscription.md) (settings.json model field)
**Implements:** the source-side fixes for the 2026-05-31 production outage

---

## Problem

On the morning of 2026-05-31 a production outage took the morty agent on
joris-cx33 offline. Post-mortem found **four latent failure modes** that the
platform source did not prevent, plus a deploy-coverage gap. The directive: the
fix must live in the platform SOURCE (pyinfra tasks + templates + this SPEC),
NOT as a manual one-off edit on the box, so that

1. a clean re-deploy makes every department identical and healthy, and
2. a future department inherits the corrected setup automatically.

This SPEC codifies the **canonical per-department agent setup invariants** — the
exact configuration every `claude-agent-<persona>.service` must have — and
documents the failure modes each invariant prevents.

---

## Canonical invariants

Every department's on-box agent MUST satisfy all five invariants below. They are
enforced in source (pyinfra tasks + jinja templates) and guarded by tests in
`lib/test_agent_layer.py` + `lib/test_telegram_watchdog.py`.

### Invariant 1 — settings.json model set to the auto-upgrading `opus[1m]` alias

- **Value:** `model = "opus[1m]"` — the auto-upgrading `opus` family alias +
  the `[1m]` 1M-context modifier. This is NOT a pinned version.
- **Source of truth:** `pyinfra/tasks/agent/_settings.py` →
  `CANONICAL_MODEL = "opus[1m]"`. The render fallback is
  `cfg.agent.llm.model if cfg.agent.llm.model else CANONICAL_MODEL`, so a tenant
  may override via `agent.llm.model` but an empty value inherits this alias.
- **Why we do NOT pin a version:** auto-upgrade is a deliberate requirement from
  Joris. The bare `opus` alias auto-resolves to the LATEST Opus release, so when
  Opus 4.9+ ships our headless agents pick it up automatically. Pinning a
  version (e.g. `claude-opus-4-8[1m]`) would strand every agent on an old Opus
  long after a newer one exists — exactly what we want to avoid. The `[1m]`
  modifier keeps the 1M-context window. Verified on the live box: `opus[1m]`
  resolves to "Claude Opus 4.8 (1M context)" today AND will track newer Opus
  releases; it is the exact alias morty's working ExecStart uses.
- **Why not `"default"` or bare `"opus"`:** the literal `"default"` was the
  actual broken value behind the 2026-05-31 outage — claude errored
  `There's an issue with the selected model (default). It may not exist` and the
  session never started (the Telegram plugin never spawned; the agent looked
  `active` to systemd but was functionally dead). Bare `opus` without the `[1m]`
  modifier resolves but loses the 1M-context window, so we require the modifier.
- **Failure mode prevented:** *model-default* — an unresolvable model bricks the
  agent on next restart.
- **Guard:** `test_settings_json_template_pins_canonical_model`,
  `test_settings_json_template_never_renders_default_or_bare_opus`,
  `test_settings_default_falls_back_to_canonical_model_via_module_constant`.

### Invariant 2 — workspace-trust seeded for the agent's cwd

- **Value:** `~/.claude.json` has
  `projects.<workdir>.hasTrustDialogAccepted = true`, where `<workdir>` follows
  the **two-tier workdir convention** in Invariant 6 below:
  - concierge tier → `/home/claude/agents/<name>` (UNPREFIXED)
  - department tier → `/home/claude/agents/bubble-ops-<slug>` (PREFIXED)
- **Source of truth:** `pyinfra/tasks/agent/_settings.py` (`_trust_seed_command`
  + the fact-gated seeding op). Idempotent: a pre-check skips the mutation when
  the trust value is already correct.
- **Why:** on first run in an unfamiliar directory claude shows a TTY-blocking
  "Quick safety check: Is this a project you created…" workspace-trust dialog.
  The supervised systemd service has no operator to click "Yes"; claude blocks
  forever.
- **Failure mode prevented:** *trust-modal freeze* — agent active but blocked on
  an undismissable prompt.

### Invariant 3 — auth via `~/.claude/.credentials.json` (subscription model)

- **Value:** the agent authenticates using the Claude Code subscription login
  persisted at `/home/claude/.claude/.credentials.json` (mode 0600), established
  once interactively on the box (`claude login`). `agent.llm.auth_mode =
  claude_code_subscription`, `provider = anthropic`.
- **NOT:** an env var `CLAUDE_CODE_OAUTH_TOKEN` shipped via SOPS, and NOT an
  OpenRouter/Anthropic API key in settings.json (that design was abandoned in
  SPEC-009). The systemd unit's `EnvironmentFile` carries `TELEGRAM_BOT_TOKEN`
  (and other channel/telemetry secrets) but the LLM auth lives in the
  credentials file.
- **Why:** subscription auth is the supported headless model; it survives plugin
  updates and avoids baking an API key into the deploy.
- **Failure mode prevented:** *fleet-wide 401* (in combination with Invariant 5b)
  — when the subscription token expires/rotates, the agent emits
  `401 Invalid authentication credentials` / `Please run /login`. A restart
  cannot fix this; re-auth (refresh of `.credentials.json`) is required. See
  Invariant 5b for the detection + alert behavior.

### Invariant 4 — per-persona Telegram watchdog installed

A `telegram-watchdog` systemd timer (every 5 min) runs
`/home/claude/scripts/telegram-watchdog.sh` for the department's
`claude-agent-<persona>.service`. The script:

- **(a) bot.pid as the primary, per-persona health signal.** The Telegram MCP
  plugin (`server.ts`) stores ALL of its per-agent state — `access.json`,
  `approved/`, `inbox/`, `.env`, AND `bot.pid` — under the directory it reads
  from `TELEGRAM_STATE_DIR`, defaulting to `~/.claude/channels/telegram` when
  unset; `bot.pid = join(STATE_DIR, 'bot.pid')`. The channel dir is therefore
  **per-persona**:
  - `morty` → `/home/claude/.claude/channels/telegram/bot.pid` (the plugin's
    built-in default — morty is the original agent, keeps the bare dir)
  - any other department → `/home/claude/.claude/channels/telegram-<persona>/bot.pid`

  **Single source of truth:** `lib/host_helpers.py` →
  `telegram_channel_dir_name()` / `telegram_channel_dir()` /
  `telegram_bot_pid_file()`. BOTH the plugin-state-dir creation
  (`pyinfra/tasks/agent/_telegram_plugin.py`) AND the watchdog
  (`pyinfra/tasks/access/telegram_watchdog.py`) derive the path from this one
  helper so they can never disagree about where `bot.pid` lives.
  - **Failure mode prevented:** *zombie poller* mis-detection — the old
    hardcoded `.../channels/telegram/bot.pid` constant pointed every department's
    watchdog at morty's bot.pid, so on a multi-agent box a non-morty watchdog
    read the wrong agent's liveness marker.

- **(b) cgroup-scoped bun-poller liveness (detection signal #3).** The watchdog
  verifies a `bun` process exists **within THIS service's cgroup**
  (`systemctl show <service> -p MainPID` + a scan of `pgrep -x bun` filtered by
  `/proc/<pid>/cgroup` naming the service). The old broad
  command-line pgrep matched ANY agent's poller and returned a false "alive" for
  a dead agent.
  - **Failure mode prevented:** *zombie poller* false-negative — a broken agent
    left unrecovered because another agent's poller was still running.

- **(c) stop→start recovery (NOT restart).** Recovery is
  `sudo systemctl stop <service>; sleep 3; rm -f <bot.pid>; sudo systemctl start
  <service>`. A bare `systemctl restart` left a zombie bun child alive that held
  the `getUpdates` slot and never re-wrote `bot.pid`, so the agent came back
  `active` but deaf. A clean stop tears down the whole cgroup (killing the
  zombie); removing the stale `bot.pid` prevents the next health check from being
  fooled by a leftover file. The sudoers drop-in grants `stop`+`start` (retaining
  `restart`/`is-active` for backward compatibility), each pinned to the exact
  unit name with no wildcards or shell metacharacters.
  - **Failure mode prevented:** *zombie poller* — agent active-but-deaf after a
    restart that didn't fully tear down the plugin.

- **(d) 401 → alert, don't loop (detection signal #5b).** BEFORE the
  stop→start recovery, the watchdog tails the newest session jsonl at
  `/home/claude/.claude/projects/<workdir-with-slashes-as-dashes>/*.jsonl` (the
  transcript dir name claude derives from the agent's cwd by replacing every
  `/` with `-`). **This path MUST track the two-tier workdir convention
  (Invariant 6):** for a concierge agent it is
  `-home-claude-agents-<name>`; for a **department** it is the PREFIXED
  `-home-claude-agents-bubble-ops-<slug>`. Deriving it from the unprefixed
  slug for a department is a silent failure — the probe tails a directory that
  never exists, 401 detection never fires, and the watchdog restart-loops
  forever against bad credentials. The bubble-ops-loop dept watchdog renders
  this path from the SAME `${DEPT_WORKDIR}` source of truth as
  `WorkingDirectory=` (`/`→`-`), so the two cannot drift. If it finds
  `401 Invalid authentication credentials` or `Please run /login`, it sends a
  DISTINCT operator alert ("AUTH 401 — agent needs re-auth, restart won't help")
  and exits **without restarting**, respecting the same cooldown window so a
  persistent 401 alerts at most once per window.
  - **Failure mode prevented:** *fleet-wide 401* — a restart loop that thrashes
    the service (and, on a fleet sharing one subscription, every department at
    once) when the real fix is re-auth.

- **Token hygiene (SPEC-008):** `TELEGRAM_BOT_TOKEN` is read from the tmpfs
  runtime env file into a shell var, used only in HTTPS curl URLs to
  `api.telegram.org`, and `unset` immediately after every use. Never echoed,
  logged, or written to disk. Guarded by the static tests in
  `lib/test_telegram_watchdog.py`.

### Invariant 5 — uniform deploy coverage across all departments

`deploy.py` invokes `telegram_watchdog.apply()` (and the full agent layer) for
**every** host pyinfra runs against; `inventory.py` enumerates one tenant
(`TENANT=<name>`) or all (`TENANTS_ALL=1`). There is **no persona-specific gate**
in `telegram_watchdog.apply()` — the only early-returns are when the secrets
layer is disabled or `contact.primary_telegram_user_id` is unset (legitimate: the
watchdog cannot alert without either). The watchdog running "only for morty" at
the time of the outage was a **deploy-coverage gap** (only morty's tenant had
been deployed), not a source gate.

- **Action:** to bring a department under the watchdog, deploy its tenant
  (`TENANT=<tenant> pyinfra inventory.py deploy.py --sudo -y`, or `TENANTS_ALL=1`
  for the whole fleet). No source change is required to add coverage.

### Invariant 6 — two-tier workdir naming (concierge UNPREFIXED, department PREFIXED)

Bubble Cabinet has a **deliberate TWO-TIER agent model** with TWO workdir
naming conventions. This is INTENTIONAL design (NORTH-STAR), **not drift**:

| Tier | Role | Examples | Workdir | Project-dir (`/`→`-`) |
|------|------|----------|---------|------------------------|
| **Concierge** (Layer 1, operator-managed) | cross-cutting, no single mandate | Morty, Claudette, or a client-named "Sandra" | `/home/claude/agents/<name>` — **UNPREFIXED** | `-home-claude-agents-<name>` |
| **Department** (Layer 2, éclos bubble-ops) | a department WITH a mandate | Tony, CGP, Maya, Ben, Miranda, Eliot | `/home/claude/agents/bubble-ops-<slug>` — **PREFIXED** | `-home-claude-agents-bubble-ops-<slug>` |

- **The `bubble-ops-` prefix is the DEPARTMENT marker.** A department's on-box
  workdir dirname always carries it; a concierge's never does.
- **Slug ≠ workdir-dirname (deliberate decoupling).** Only the workdir dirname
  is prefixed. The SLUG continues to drive — UNPREFIXED — the service name
  (`ops-loop-<slug>`), the Telegram state dir (`telegram-<slug>`), the runtime
  env dir (`/run/claude-agent-<slug>`), and the SOPS secrets file
  (`secrets-<slug>`). Confirmed on the live box: slug and workdir-dirname are
  intentionally separate. Do NOT prefix the slug-derived names.
- **Single source of truth.** In the bubble-ops-loop department deploy path
  (`scripts/deploy-to-morty.sh`) the prefix lives in exactly ONE variable,
  `DEPT_WORKDIR="/home/claude/agents/bubble-ops-${SLUG}"`, surfaced into the
  systemd template via a `${DEPT_WORKDIR}` placeholder (used by
  `WorkingDirectory=`) and into the watchdog via `${DEPT_WORKDIR}` +
  `${DEPT_WORKDIR_PROJECTDIR}` (the `/`→`-` form for the 401 probe). The trust
  seed (Invariant 2), the repo clone target, `WorkingDirectory=`, and the
  401-detection session-dir (Invariant 4d) all derive from this one variable, so
  they cannot disagree.
- **Parameterize names — no hardcoding (NORTH-STAR).** The tier and the prefix
  must be parameterized so the Tenant-as-a-Service / Bubble Local packaging
  works for ANY client. Never hardcode persona names ("morty"/"claudette") or
  the operator ("Joris"); a client's concierge could be "Sandra". Only the
  literal `bubble-ops-` department marker is fixed (it is the convention itself).
- **Failure mode prevented:** *workdir mismatch* — a department deployed to the
  unprefixed `/home/claude/agents/<slug>` while its real working copy lives at
  `…/bubble-ops-<slug>` (or vice versa) → systemd starts in an empty dir and
  crash-loops; AND the watchdog's 401 probe tails a non-existent transcript dir
  → 401 detection silently never fires.
- **Guard:** `tests/test_systemd_path_matches_deploy.py`
  (`test_dept_workdir_is_prefixed_with_bubble_ops`,
  `test_slug_decoupled_from_workdir_dirname`),
  `tests/test_deploy_trust_seed.py`
  (`test_dry_run_trust_seed_uses_prefixed_dept_workdir`),
  `tests/test_telegram_watchdog.py`
  (`test_401_session_dir_uses_prefixed_dept_workdir`) in bubble-ops-loop.

---

## Architecture note — multi-agent per box (DELIVERED, SPEC-001 v1.2)

**UPDATE 2026-05-31 (multi-concierge refactor):** the watchdog units, script
path, sudoers drop-in, and RuntimeDirectory are now **PERSONA-SUFFIXED**
(`telegram-watchdog-<name>.{timer,service}`,
`/home/claude/scripts/telegram-watchdog-<name>.sh`,
`/etc/sudoers.d/claude-telegram-watchdog-<name>`, `/run/telegram-watchdog-<name>/`).
This realizes the multi-agent-per-box follow-up flagged below. Both the agent
layer and the watchdog now LOOP over `cfg.agent.concierges` (SPEC-001 v1.2), so
multiple concierges (morty + claudette) coexist on one box with zero unit-name
collisions.

**Migration choice — migration-b (uniform suffixing):** we suffix EVERY
concierge, INCLUDING morty (no "morty is special" branch). A regular scheme is
simpler and matches the bubble-ops-loop department watchdogs and claudette's
interim hand-deploy. The cost is a **one-time cleanup** of morty's OLD
un-suffixed units on the live box at cutover:

```
sudo systemctl disable --now telegram-watchdog.timer
sudo rm -f /etc/systemd/system/telegram-watchdog.{timer,service} \
           /etc/sudoers.d/claude-telegram-watchdog \
           /home/claude/scripts/telegram-watchdog.sh
sudo systemctl daemon-reload
# (the next deploy installs telegram-watchdog-morty.* in their place)
```

Single-source helpers in `lib/host_helpers.py` (`watchdog_unit_basename`,
`agent_service_name`, `agent_workdir`, `agent_session_projects_dir`,
`runtime_env_dir`, `runtime_env_file`) keep every per-concierge artifact derived
from the concierge NAME, so two concierges can never disagree about their own
paths. Guarded by
`lib/test_telegram_watchdog.py::TestMultiConciergeWatchdogSuffixing` and
`lib/test_agent_layer.py::TestMultiConciergeAgentLayer`.

**(Historical, pre-refactor:** the watchdog units used fixed non-persona-suffixed
names — correct for the then-current one-agent-per-box model. The bot.pid +
401-probe paths were already per-persona, so only the unit names needed
suffixing. The cutover was deliberately deferred out of the incident fix.)

---

## Cross-references

- **SPEC-007** — agent install / systemd unit layout.
- **SPEC-009** — Claude Code subscription auth model (Invariant 3) and the
  settings.json `model` field (Invariant 1).
- **SPEC-013** — original Telegram recovery watchdog. Invariants 4(a)–(d)
  **extend/supersede** SPEC-013's detection signal #3, bot.pid path, recovery
  action, and sudoers scope. See the banner at the top of SPEC-013.

---

## Acceptance criteria

1. A clean deploy of any tenant renders `settings.json` with
   `"model": "opus[1m]"` (the auto-upgrading alias, NOT a pinned version) and
   never `"default"` / bare `"opus"` without the `[1m]` modifier.
2. The watchdog rendered for persona X references
   `telegram-X/bot.pid` (or bare `telegram/bot.pid` for morty), derived from the
   shared helper.
3. The watchdog uses a cgroup-scoped bun-poller check (no broad pgrep).
4. The watchdog recovery is stop→start (with `rm -f bot.pid` between), and the
   sudoers drop-in permits stop+start.
5. The watchdog detects 401/`Please run /login` in the agent's transcript and
   alerts-without-restarting, once per cooldown.
6. A department deploy (bubble-ops-loop `deploy-to-morty.sh`) places the agent
   workdir at the PREFIXED `/home/claude/agents/bubble-ops-<slug>`, derives the
   401-probe session-dir as `-home-claude-agents-bubble-ops-<slug>` from the
   same source of truth, and keeps the slug-derived service/env/channel/secrets
   names UNPREFIXED (Invariant 6). A concierge deploy keeps the workdir
   UNPREFIXED. Both tiers are parameterized — no persona/operator names are
   hardcoded.
7. All of the above are covered by green tests in `lib/test_agent_layer.py` and
   `lib/test_telegram_watchdog.py` (platform side) and the bubble-ops-loop
   guards listed under Invariant 6.
