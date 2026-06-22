# SPEC-007 — Agent install + systemd unit (Step 4)

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending {{OPERATOR}} approval_
**Depends on:** SPEC-005 (hardening done), SPEC-006 (secrets layer done)
**Implements:** Step 4 of the Bubble VPS Platform build plan

---

## Purpose

Replace the ad-hoc `start-claude-agent.sh` + `telegram-mcp-wrapper.sh` shell scripts with a proper systemd service that:

1. Installs Claude Code, Node.js, Bun on the tenant's box (idempotent)
2. Materializes `~/.claude/settings.json` from a template (NO secrets baked in — everything via env vars from the SOPS-decrypted EnvironmentFile)
3. Drops a systemd unit `claude-agent-<persona>.service` that:
   - `ExecStartPre` decrypts secrets via `sops` into `/run/claude-agent/env`
   - `EnvironmentFile=/run/claude-agent/env` exposes them to the agent process
   - `ExecStart` runs `claude --channels plugin:telegram@claude-plugins-official`
   - `Restart=on-failure`, `RestartSec=10`, `RunAtLoad`-equivalent (`WantedBy=multi-user.target`)
4. Starts the service (and ensures it survives reboot)
5. Wipes the legacy plaintext scripts (`start-claude-agent.sh`, `telegram-mcp-wrapper.sh`, `~/.secrets`)

This makes the agent **always-on, supervised, and reboot-survivable** — replacing the current tmux setup which has none of those properties.

---

## Architecture

```
┌──── Tenant box ──────────────────────────────────────┐
│                                                       │
│  /etc/systemd/system/claude-agent-ricky.service       │
│     ExecStartPre=/usr/local/bin/sops --decrypt        │
│         --output /run/claude-agent/env                │
│         /etc/bubble/secrets.sops.env                  │
│     EnvironmentFile=/run/claude-agent/env             │
│     ExecStart=/usr/bin/claude --channels ...          │
│     User=claude                                       │
│     Group=claude                                      │
│     WorkingDirectory=/home/claude/agents/ricky        │
│     Restart=on-failure                                │
│     RestartSec=10                                     │
│     LimitNOFILE=65536                                 │
│                                                       │
│  /home/claude/agents/<persona>/                       │
│     ├── CLAUDE.md  ← rsynced from data repo persona   │
│     └── workspace/ ← rsynced from data repo persona   │
│                                                       │
│  /home/claude/.claude/                                │
│     ├── settings.json  ← templated (NO SECRETS!)      │
│     ├── plugins/                                      │
│     └── channels/telegram/                            │
└───────────────────────────────────────────────────────┘
```

---

## settings.json template (the critical change)

The current settings.json has the OpenRouter key baked in:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-f3b848...REDACTED"  ← BAD
  }
}
```

The new template references env vars instead — vars come from EnvironmentFile at service start:

```json
{
  "enabledPlugins": {
    "telegram@claude-plugins-official": true
  },
  "permissions": {
    "defaultMode": "acceptEdits"
  },
  "env": {
    "ANTHROPIC_BASE_URL": "{{ agent.llm.base_url }}",
    "ANTHROPIC_AUTH_TOKEN": "${OPENROUTER_API_KEY}",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "{{ agent.llm.model }}",
    "PATH": "/home/claude/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  }
}
```

**Key insight:** Claude Code's settings.json supports `${VAR}` shell-style env-var expansion at process start. `OPENROUTER_API_KEY` is exposed by the systemd EnvironmentFile, expanded at agent start, never written to disk.

`{{ agent.llm.base_url }}` and `{{ agent.llm.model }}` are jinja2 substitutions at template time — not secret, fine to bake in.

---

## Telegram bot token handling

Same pattern. The plugin reads `TELEGRAM_BOT_TOKEN` from env. The plugin's `.env` file at `~/.claude/channels/telegram/.env` becomes a symlink (or stub) referencing the systemd-injected env var:

Option A (symlink): `~/.claude/channels/telegram/.env` → `/run/claude-agent/env`
   - Pros: zero plaintext. Cons: fragile (plugin may not handle non-key=value content well)

Option B (stub): `~/.claude/channels/telegram/.env` contains literally `TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}` — the plugin's loader expands it at runtime.
   - Pros: simpler. Cons: plugin loaders may not all support env expansion in .env values.

Option C (manage via plugin config): some plugins expose token via env var directly (no .env file needed). Test which works for our Telegram plugin.

**Recommended:** Test C first; fall back to B if needed. Document in implementation report.

---

## Tasks layout

```
bubble-vps-platform/pyinfra/tasks/agent/
├── __init__.py
├── deploy.py                ← public entrypoint
├── _install.py              ← installs node, bun, claude-code (idempotent)
├── _persona.py              ← rsync persona from data repo
├── _settings.py             ← templates settings.json
├── _telegram_plugin.py      ← installs/configures Telegram plugin
├── _systemd.py              ← drops the .service file, daemon-reload, enable+start
└── _cleanup_legacy.py       ← removes the old shell scripts + plaintext .secrets
```

---

## Idempotency rules (same as Step 2)

- Software installs only run if missing (apt, bun installer, npm install -g)
- Templates write only if hash changed
- systemd unit reload only if file changed
- Service restart only if EnvironmentFile changed (which itself depends on encrypted file changing)
- Persona rsync uses `rsync -a --delete` so persona changes propagate cleanly

---

## Service unit (full)

```ini
# /etc/systemd/system/claude-agent-{{ persona.name }}.service
[Unit]
Description=Claude Agent — {{ persona.name }} ({{ tenant.tenant_name }})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=claude
Group=claude
WorkingDirectory=/home/claude/agents/{{ persona.name }}

# Decrypt secrets into tmpfs at startup (root, not the service user, since age key is root-only)
PermissionsStartOnly=true
ExecStartPre=+/bin/mkdir -p /run/claude-agent
ExecStartPre=+/bin/chown claude:claude /run/claude-agent
ExecStartPre=+/usr/local/bin/sops --decrypt --output /run/claude-agent/env-tmp /etc/bubble/secrets.sops.env
ExecStartPre=+/bin/mv /run/claude-agent/env-tmp /run/claude-agent/env
ExecStartPre=+/bin/chmod 0400 /run/claude-agent/env
ExecStartPre=+/bin/chown claude:claude /run/claude-agent/env

EnvironmentFile=/run/claude-agent/env
ExecStart=/usr/bin/claude --channels plugin:telegram@claude-plugins-official

Restart=on-failure
RestartSec=10
LimitNOFILE=65536

# Secrets file should be removed when service stops
ExecStopPost=+/bin/rm -f /run/claude-agent/env

[Install]
WantedBy=multi-user.target
```

**Notes on the `+` prefix:** systemd's `+` runs the ExecStartPre as root (overriding User=claude), which is needed to read `/etc/age/key.txt`. The decrypted file is then chowned to `claude` so the agent process can read it.

---

## Bootstrap order (deploy.py)

```python
# pyinfra/deploy.py
from pyinfra import host
from pyinfra.tasks.hardening import linux as hardening
from pyinfra.tasks.secrets import deploy as secrets
from pyinfra.tasks.agent import deploy as agent

# Order matters:
hardening.apply()  # Step 2
secrets.apply()    # Step 3 (depends on hardening)
agent.apply()      # Step 4 (depends on secrets — agent needs decrypted env)
```

If Step 3's `_age_setup` needs operator-manual `.sops.yaml` update (first-bootstrap), the deploy will halt cleanly there — Step 4's task won't try to start a service that can't decrypt.

---

## Test plan for Step 4

### Unit tests
- `test_settings_json_no_plaintext_keys` — render settings.json template, grep for `sk-or-v1`, `8350575119`, etc. Must not appear.
- `test_systemd_unit_template_renders` — compare to golden file
- `test_envfile_decryption_command` — verify the ExecStartPre includes the right sops invocation

### Integration
- `tests/integration/test_agent_dogfood.sh`:
  ```bash
  # Run full deploy
  ./scripts/deploy.sh --tenant=bubble-internal

  # Verify on box
  ssh hetzner '
      systemctl is-active claude-agent-ricky.service && echo "✅ service active"
      systemctl is-enabled claude-agent-ricky.service && echo "✅ service enabled"
      test -f /run/claude-agent/env && echo "✅ env file present"
      [ "$(stat -c %a /run/claude-agent/env)" = "400" ] && echo "✅ env file mode 0400"

      # Verify NO plaintext secrets remain in expected leak locations
      ! grep -l "sk-or-v1" /home/claude/.secrets /home/claude/start-claude-agent.sh /home/claude/.claude/settings.json 2>/dev/null && echo "✅ no OR key plaintext"
      ! grep -l "8350575119:AAH" /home/claude/.claude/channels/telegram/.env 2>/dev/null && echo "✅ no bot token plaintext"

      # Smoke test: send a Telegram message; expect a response
      # (manual; documented in INSTALL.md)
  '
  ```

- Reboot test: `ssh hetzner sudo reboot`, wait 60s, verify service comes back online without intervention.

---

## Acceptance criteria for Step 4

1. ✅ Claude Code, Node.js, Bun installed on box (idempotent — no-op if already present at correct version)
2. ✅ `~/.claude/settings.json` rendered from template, contains NO plaintext secrets, all `${VAR}` references resolve from EnvironmentFile
3. ✅ systemd unit `claude-agent-<persona>.service` exists, is enabled + active
4. ✅ Service decrypts secrets into `/run/claude-agent/env` (tmpfs) at start, removes them at stop
5. ✅ All 8 legacy plaintext files (the leak set) are removed
6. ✅ Reboot survives (service auto-starts)
7. ✅ Smoke test: Telegram message → reply works
8. ✅ Idempotent re-deploy: zero changes
9. ✅ tmux session for the old agent is killed (no longer needed — systemd is the supervisor)

---

## Migration path ({{VPS_HOST}} specifically)

Currently {{VPS_HOST}} has:
- `~/start-claude-agent.sh` running in tmux session `claude-agent`
- `~/.claude/settings.json` with plaintext OR key
- `~/.claude/channels/telegram/.env` with plaintext bot token
- 6 other plaintext copies (see Step 1 audit)

Migration sequence (during Step 4 deploy):
1. Step 4 deploy installs systemd unit with NEW (encrypted-env-driven) settings
2. Validation step verifies new service can start successfully
3. Old tmux session killed: `tmux kill-session -t claude-agent`
4. Cleanup removes 8 plaintext files
5. New systemd service starts (it was `enabled` but not yet `started` until cleanup verified)

If anything fails between steps 2-4, we have a roll-forward path: re-run deploy, fix the issue. We do NOT delete plaintext files until the encrypted path is proven working.

---

## Open questions

1. **What's the persona content for the first deploy?** Empty CLAUDE.md (placeholder) or a real Ricky persona moved from the Mac? Recommendation: empty placeholder for Step 4. Step 5 ("Persona") fills it for real with the agent identity decision {{OPERATOR}} hasn't made yet.

2. **Should we install Claude Code from npm or from a checked-in binary?** Recommendation: npm (`npm install -g @anthropic-ai/claude-code`). Pinned version in tenant.yaml's `agent.install.claude_code_version`.

3. **What about the plugin marketplace?** Telegram plugin lives in `~/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6/`. This gets re-downloaded on first claude run if missing. Need to test: does pyinfra need to pre-warm this, or does claude do it on demand? Probably on-demand. Document and test.

4. **Logs**: where does the agent's stdout go? systemd captures to journald by default. `journalctl -u claude-agent-ricky.service -f` for tailing. Document.

5. **Multi-agent on one box** (future, e.g. Ricky + Lab on the same cx33): each becomes its own systemd unit (`claude-agent-ricky.service`, `claude-agent-lab.service`). Distinct Telegram bot tokens (per the 409-conflict rule we already know). Distinct EnvironmentFile decryption (they each get their own /run/claude-agent-<name>/env). Support this in the unit template by parameterizing the persona name in all paths. Defer the actual second-agent deploy to a later step but design the templates to allow it.

---

## Cross-refs

- `~/claude-workspaces/rnd/projects/hetzner-migration/CLAUDE_AGENT_RECIPE.md` — current plaintext-key recipe we're replacing
- `~/.claude/agent-memory/shared-wiki/shared/systems/persistent-claude-agent-vps.md` — wiki page documenting current state
- SPEC-005, SPEC-006 — must be complete before Step 4 starts
