# SPEC-009 — Step 4 addendum: claude_code_subscription auth mode

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Replaces** the auth-related portions of SPEC-007. SPEC-007's overall structure remains valid; this doc updates the credential-handling sections to reflect the current (post-2026-05-08-Joris-decision) auth model.

---

## What changed since SPEC-007 was written

SPEC-007 assumed `provider: openrouter` with `OPENROUTER_API_KEY` in SOPS. Joris pivoted (msg 1640) to `provider: anthropic` with `auth_mode: claude_code_subscription`. The agent now uses a long-lived OAuth token from `claude setup-token` instead of an API key.

The change is small in spirit but significant in the settings.json template.

---

## Updated settings.json template

```json
{
  "enabledPlugins": {
    "telegram@claude-plugins-official": true
  },
  "permissions": {
    "defaultMode": "acceptEdits"
  },
  "env": {
    "PATH": "/home/claude/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  }
}
```

**What's NOT in the template anymore:**
- `ANTHROPIC_BASE_URL` (no longer routing through OpenRouter — uses Anthropic native)
- `ANTHROPIC_AUTH_TOKEN` (no longer using API-key auth)
- `ANTHROPIC_API_KEY` (same)
- `ANTHROPIC_MODEL` (the OAuth token's account already has model entitlements; Claude Code picks the right one)

**What lives where:**
- `CLAUDE_CODE_OAUTH_TOKEN` — exposed via systemd EnvironmentFile (`/run/claude-agent/env`) → environment variable when claude starts → Claude Code authenticates with Anthropic's API using subscription credentials
- `TELEGRAM_BOT_TOKEN` — same path, used by the Telegram plugin

The settings.json itself contains NO secrets, NO env-var references, NO model identifier. It's purely declarative configuration: which plugins enabled, what permissions mode, what PATH. Safe to commit anywhere.

## Why the template doesn't need ANTHROPIC_MODEL

Per the [Claude Code authentication docs](https://code.claude.com/docs/en/authentication), when authenticated via OAuth (`CLAUDE_CODE_OAUTH_TOKEN`), Claude Code uses the model entitled by the subscription. The user's account tier (Pro/Max/Team/Enterprise) determines available models; the CLI selects automatically. We don't need to (and shouldn't) pin a model in settings.json — it would override the entitlement-based selection.

If we ever DO want to pin the model (e.g. to control cost), we'd add `ANTHROPIC_MODEL` to settings.json env block as a separate step. NOT for Step 4.

## Updated systemd unit (excerpt — only the env-related parts changed)

Same as SPEC-007 §"Service unit (full)", with this key invariant:

The `EnvironmentFile=/run/claude-agent/env` directive exposes both `CLAUDE_CODE_OAUTH_TOKEN` and `TELEGRAM_BOT_TOKEN` to the `claude` process. Claude Code automatically reads `CLAUDE_CODE_OAUTH_TOKEN` per the [authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence) (priority 5 in their list, used when no `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` and no `/login` interactive session).

This is the cleanest path: agent boots, systemd injects env vars, claude picks up the OAuth token without any code knowing where it came from.

## What stays the same as SPEC-007

- File paths (`/etc/bubble/secrets.sops.env`, `/run/claude-agent/env`, `/etc/age/key.txt`)
- systemd unit shape (User=claude, Group=claude, ExecStartPre+ chain for sops decrypt, Restart=on-failure, WantedBy=multi-user.target)
- Persona rsync from `bubble-vps-data/tenants/<name>/persona/` → `/home/claude/agents/<name>/`
- Plaintext cleanup AFTER verification passes (the 8 known leak files)
- tmux session kill at the end

## Acceptance criteria delta

Step 4's acceptance criteria from SPEC-007 still apply, with these specific updates:

1. ✅ `~/.claude/settings.json` rendered with NO secrets and NO `${VAR}` references in the env block — pure declarative config
2. ✅ systemd unit's `EnvironmentFile=/run/claude-agent/env` exposes `CLAUDE_CODE_OAUTH_TOKEN` + `TELEGRAM_BOT_TOKEN`
3. ✅ Agent process authenticates via OAuth token automatically (no `claude login` required, no Keychain access)
4. ✅ Smoke test: send a Telegram message → agent replies (Claude Code receives the prompt, calls Anthropic API via OAuth token, generates response, replies via Telegram plugin which reads the bot token from env)

## Open question for the subagent

**The Telegram plugin's `.env` handling** — SPEC-007 listed three options (A symlink, B stub-with-env-expansion, C plugin reads env directly). Need to test which actually works with the plugin v0.0.6 we use.

Recommend the subagent:
1. Try option C first (plugin reads `$TELEGRAM_BOT_TOKEN` from systemd-injected env, no `.env` file at all). Cleanest.
2. If the plugin requires a `.env` file specifically, use option B (stub with env-expansion) — the plugin's bun-side loader should handle `${VAR}` expansion at runtime.
3. If neither works, fall back to option A (symlink to `/run/claude-agent/env`) — fragile but works as last resort.

Document what works in the implementation report.

## Durable headless authentication (the long-lived OAuth token model)

> **The durable auth model.** This is the canonical answer to "how does a
> headless VPS agent stay logged in to Claude without a human re-auth-ing it
> every day?" Short answer: the long-lived `CLAUDE_CODE_OAUTH_TOKEN` delivered
> through the SOPS blob → the agent's runtime env file (as described above) —
> **not** a hand-ported `~/.claude/.credentials.json`.

### Why `.credentials.json` is not durable on a server

Claude Code does **not** auto-refresh its OAuth token in non-interactive
(headless) mode. The `refreshToken` stored in `~/.claude/.credentials.json` is
only exercised by the interactive client; on a `systemd`-managed server the
`accessToken` simply expires (typically ~daily) and the agent then 401s on its
next API call. (Anthropic GitHub issues #50743 and #28827.)

The historical band-aid was to copy Joris's Mac `~/.claude/.credentials.json`
onto the box. It is brittle: it expires within a day and *races the Mac's own
refresh* (the Mac rotates the token out from under the copy), needing a human in
the loop on a roughly daily cadence.

### The durable source: `CLAUDE_CODE_OAUTH_TOKEN` from the blob

`claude setup-token` produces a **long-lived (~1-year), self-refreshing** token
intended for CI / headless use, consumed via the `CLAUDE_CODE_OAUTH_TOKEN` env
var (the same var this addendum already wires). Server-side it refreshes itself,
so no interactive client and no `.credentials.json` are needed. The token flows
exactly as §"Updated systemd unit" describes: it is a key in the SOPS blob
(listed in `secrets.required_keys`), decrypted into `/run/claude-agent[-<name>]/env`,
and loaded via `EnvironmentFile=-`.

The Step-4 verification gate (`pyinfra/tasks/agent/_verify.py`, check #5)
asserts **per concierge**, name-only (never the value), that
`CLAUDE_CODE_OAUTH_TOKEN` is present in the on-disk runtime env file — so a
deploy *proves* the durable token is wired before `_cleanup_legacy` runs. The
gate checks it for **both** the primary and every remapped non-primary concierge
(the per-concierge remap only swaps the Telegram bot-token ref).

### Precedence gotcha — why the env token can be "inert"

Empirically verified on the live box: **`claude` PREFERS
`~/.claude/.credentials.json` when present.** A bogus env token + a present valid
creds file → still authenticates via the creds file. The creds file absent + a
valid `CLAUDE_CODE_OAUTH_TOKEN` in env → authenticates fine via env. So the env
token only takes effect when `.credentials.json` is **not** the winning source.
A stray/stale `.credentials.json` silently shadows the durable env token — then
expires and 401s, the exact failure we're escaping.

### The platform invariant (guarded)

**The deploy never writes a `~/.claude/.credentials.json`.**

- No task under `pyinfra/tasks/**` writes one — it is never a `files.put` /
  `files.template` `dest`, nor a `server.shell` redirect target. Regression-
  guarded by `TestDurableClaudeAuth` in `lib/test_agent_layer.py`. The only
  appearances of `.credentials.json` in `pyinfra/` are PROSE: a `tenant.yaml.j2`
  comment and the `security-audit.sh.j2` `--exclude` (so the audit doesn't flag
  the CLI's own login store).
- The operator-supplied long-lived token is ported **into the SOPS blob** as
  `CLAUDE_CODE_OAUTH_TOKEN` via `scripts/operator-set-secret.sh` (a `sops --set`
  flow), never a plaintext creds file.
- Any `.credentials.json` ever on the box was placed there **manually** (the
  band-aid). The durable cutover removes it.

The durable model is therefore: keep `CLAUDE_CODE_OAUTH_TOKEN` fresh in the blob
(self-refreshing → ~yearly chore, not daily) **and** ensure no hand-placed
`~/.claude/.credentials.json` shadows it. The one-time operator cutover is in
`docs/RUNBOOK-durable-claude-auth.md`.

## Cross-refs

- SPEC-007 — overall Step 4 design (95% still valid; this doc updates the auth section)
- SPEC-006 + SPEC-008 — secrets layer (already implemented)
- [Claude Code auth docs](https://code.claude.com/docs/en/authentication)
- `docs/RUNBOOK-durable-claude-auth.md` — operator cutover to the durable model
- Anthropic GitHub issues #50743, #28827 (no OAuth auto-refresh when headless)
