# 🫧 Bubble VPS Platform — One-Click Agent Infrastructure

**Provision a hardened VPS with AI agents in one command.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Built on Claude Code](https://img.shields.io/badge/built%20on-Claude%20Code-orange)](https://claude.ai)
[![Tests: 399 / 399 passing](https://img.shields.io/badge/tests-399%20passing-green)]()

---

## What is bubble-vps-platform?

A **pyinfra-driven provisioning platform** that turns a fresh Hetzner (or any Ubuntu) VPS into a fully-operational AI agent infrastructure. One command: `new-tenant.sh` → hardened server + concierge agent + agentic framework + monitoring + backups.

Deploy a client in 10 minutes. Zero manual SSH configuration. Production-hardened since May 2026.

## Quickstart

```bash
git clone https://github.com/Bubble-invest/bubble-vps-platform.git
cd bubble-vps-platform
# Create a tenant:
./scripts/new-tenant.sh acme-corp --display-name="Acme Corp"
# Fill in secrets:
./scripts/operator-set-secret.sh --tenant=acme-corp --key=TELEGRAM_BOT_TOKEN
./scripts/operator-set-secret.sh --tenant=acme-corp --key=CLAUDE_CODE_OAUTH_TOKEN
# Deploy:
./deploy.sh --tenant=acme-corp
```

10 minutes later: hardened VPS, concierge agent on Telegram, framework ready to hatch departments.

## What it provisions

| Layer | What | Includes |
|---|---|---|
| **Hardening** | OS security | sshd, ufw, fail2ban, ntp, sandbox (Layer B anti-prompt-injection) |
| **Secrets** | Encryption | SOPS + age, per-tenant keypairs, encrypted environment |
| **Agent** | Runtime | Claude Code, bun, persona, systemd unit, Telegram plugin |
| **Access** | Connectivity | Tailscale, telegram watchdog, phone-home, security audit |
| **Monitoring** | Observability | Dashboard, restic backup (6h), cache sync, secrets sweep, leak scan |
| **Framework** | Agents | Auto-installs bubble-ops-loop (loop timers, dept scaffolding) |

## Tenant-as-a-Service

```bash
./scripts/new-tenant.sh <name>     # Scaffold tenant config
./scripts/provision-tenant.sh      # Provision Hetzner box
./deploy.sh --tenant=<name>        # Deploy everything
./scripts/offboard-tenant.sh       # Safe decommission
```

## Companion repos

- [bubble-ops-loop](https://github.com/Bubble-invest/bubble-ops-loop) — the agent framework
- [bubble-cabinet](https://github.com/Bubble-invest/bubble-cabinet) — Docker on-prem deployment

## License

MIT © 2026 Bubble Invest
