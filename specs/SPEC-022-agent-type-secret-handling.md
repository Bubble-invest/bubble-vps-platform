# SPEC-022 — Agent-Type Secret-Handling Convention

**Status:** ADOPTED · **Date:** 2026-06-03 · **Author:** Rick (R&D) · **Approved by:** {{OPERATOR}}
**Supersedes drift:** consolidates the ad-hoc per-agent secret wiring observed in the 2026-06-03 fleet audit.
**Related:** SPEC-006 (secrets-sops-age), SPEC-012 (secrets-restart-on-change), SPEC-021 (canonical-agent-setup)

## Purpose

Every agent's secret handling is determined by its **type**. Two types, two templates. New agents MUST follow the template for their type; no per-agent improvisation. This prevents the drift this spec was written to fix (a test fixture reading concierge secrets, dead Mac-keyed home-dir files shadowing nothing).

## Agent types

| Type | Definition | Members (2026-06-03) | Secret file | Recipients |
|---|---|---|---|---|
| **Concierge** | Cross-cutting agent serving the whole org; trusted with shared org-level secrets | claudette, morty | shared `/etc/bubble/secrets.sops.env` | **box key only** (`age155d7…`) — VPS-managed, not Mac-editable |
| **Department** | Single-domain agent; isolated, sees only its own secrets | ben, maya, tony, cgp | per-dept `/etc/bubble/secrets-<slug>.sops.env` | **box key + Mac key** — two-way edit ({{OPERATOR}} edits on Mac, box decrypts) |

**Non-agents** (test fixtures, e.g. `fixture`) are NOT a type. They MUST NOT read either secret store. Keep them `systemctl disable`d.

## The one runtime pipeline (identical for both types)

```ini
User=claude
# root-context decrypt (the `+` runs ExecStartPre as root to read /etc/age/key.txt)
ExecStartPre=+/bin/mkdir -p /run/claude-agent-<slug>
ExecStartPre=+/bin/chown claude:claude /run/claude-agent-<slug>
ExecStartPre=+/bin/chmod 0750 /run/claude-agent-<slug>
ExecStartPre=+/bin/sh -c 'SOPS_AGE_KEY_FILE=/etc/age/key.txt /usr/local/bin/sops --decrypt --output /run/claude-agent-<slug>/env.tmp <SECRET_FILE>'
# token remap (see below)
ExecStartPre=+/bin/sh -c '<remap from env.tmp to env>'
ExecStartPre=+/bin/chmod 0400 /run/claude-agent-<slug>/env
ExecStartPre=+/bin/chown claude:claude /run/claude-agent-<slug>/env
EnvironmentFile=-/run/claude-agent-<slug>/env
```

- Plaintext exists ONLY on tmpfs (`/run/...`), 0400 claude-owned, purged on stop.
- Uses `--output FILE` (never stdout) → passes sops-guard regardless of trusted-units membership.
- `EnvironmentFile=-` (leading `-`) so unit-load doesn't fail before ExecStartPre creates the file.

## Telegram token remap convention

Every agent's bot token is stored under a namespaced key and remapped to the generic `TELEGRAM_BOT_TOKEN` the plugin expects:

- **Concierge:** `<NAME>_TELEGRAM_BOT_TOKEN` (e.g. `CLAUDETTE_TELEGRAM_BOT_TOKEN`) — distinct bot per concierge, so a per-name key is correct. (morty currently reads `TELEGRAM_BOT_TOKEN` direct; acceptable since it's the shared blob's primary.)
- **Department:** `DEPT_TELEGRAM_BOT_TOKEN` — generic, one per per-dept file.

The remap MUST be fail-loud: if the source key is absent, `exit 1` (don't silently blank `TELEGRAM_BOT_TOKEN` → silent Telegram death). Pattern:
```sh
if grep -q "^<SRC_KEY>=" env.tmp; then
  grep -v "^TELEGRAM_BOT_TOKEN=" env.tmp | sed "s/^<SRC_KEY>=/TELEGRAM_BOT_TOKEN=/" > env && rm env.tmp
else echo "bubble: <SRC_KEY> not found — refusing to blank TELEGRAM_BOT_TOKEN" >&2; exit 1; fi
```

## Writing/rotating a secret

Use `morty-sops-add-key` (root, stdin-only, refuses overwrite, atomic, audited) via SSH:
```sh
printf '%s' "$VALUE" | ssh hetzner-root "/usr/local/bin/morty-sops-add-key '<SECRET_FILE>' '<KEY>'"
```
- Never pass the value on argv. Never print decrypted values.
- Never dry-run a write path against a live SOPS file (osascript/SSH fires regardless — 2026-06-01 Maya incident).
- After change: `systemctl restart <unit>`, then verify `active` AND the `bun server.ts` poller + `bot.pid` came up (active ≠ working).

## Prohibited (the drift this spec kills)

1. **No Mac-keyed home-dir secret files on the VPS** (`/home/claude/agents/<slug>/secrets.sops.env`). The box can't decrypt Mac-only-keyed files → they're dead weight and confusing. Secrets the box must read live in `/etc/bubble/` keyed to the box. (Mac-side home files are fine on the Mac for local/Lab use, not on the VPS.)
2. **No department reading the shared blob.** Departments are isolated by construction.
3. **No non-agent (test fixture) reading either store.** Disable it.
4. **No `NoNewPrivileges=yes`** on a unit needing sudo (use a `no-nnp.conf` drop-in if NNP is otherwise inherited).

## Conformance (2026-06-03)

| Agent | Type | Conforms? |
|---|---|---|
| claudette | concierge | ✅ (Mac-keyed home file retired 2026-06-03; reads shared blob) |
| morty | concierge | ✅ (clean reference template) |
| ben | department | ✅ |
| maya | department | ✅ |
| tony | department | ✅ |
| cgp | department | ✅ (inactive, wired correctly) |
| fixture | non-agent | ✅ (disabled 2026-06-03) |

## Enforcement

- New-agent éclosure (SPEC-021) MUST pick a type and apply that type's template verbatim.
- A periodic audit (the read-only fleet sweep used 2026-06-03) checks: who reads the shared blob (must be concierges only), any Mac-keyed home-dir secret files (must be none), any non-agent reading a secret store (must be none).
