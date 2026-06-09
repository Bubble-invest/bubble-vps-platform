# bubble-vps-platform

**Bubble VPS Platform — sellable infrastructure-as-code for deploying always-on Claude Code agents on hardened EU VPS, with full data sovereignty and managed access via Tailscale.**

One operator Mac, two repos (this one + private `bubble-vps-data`), and N hardened Hetzner CX33 boxes — one for ourselves and one per paying client. Reproducible, auditable, drift-tested.

## Quick demo

```bash
./scripts/deploy.sh --tenant=bubble-internal
```

That single command lands the full stack on the target box: Linux hardening (UFW / fail2ban / sshd / unattended-upgrades / swap / chrony), the SOPS+age secrets layer, the Claude Code agent + systemd unit, Tailscale join, phone-home daemon, and the security-audit cron. ~8-10 min on a fresh box.

## Architecture

Two-repo split: this repo (sellable, MIT-style) carries the pyinfra task modules, templates, and operator scripts. The companion private repo `bubble-vps-data` carries `tenants/<name>/{tenant.yaml, secrets.sops.env, persona/}` per tenant. Operator Mac holds the master age key and the Hetzner / Tailscale tokens (Keychain). Each tenant box holds its own age key (root of trust for its own secrets). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## For operators

Full setup walkthrough: [docs/INSTALL.md](docs/INSTALL.md). Per-client onboarding playbook: [docs/ONBOARDING.md](docs/ONBOARDING.md). Day-2 ops playbook: [docs/RUNBOOK.md](docs/RUNBOOK.md). Threat model + key rotation: [docs/SECURITY.md](docs/SECURITY.md).

## Repo layout

- `inventory.py` — pyinfra inventory; loads tenant configs from `bubble-vps-data`
- `deploy.py` — top-level orchestration entrypoint
- `lib/` — tenant config loader, helpers, and tests (stdlib + pyyaml only)
- `pyinfra/tasks/` — task modules (hardening, secrets, agent, access, monitoring)
- `pyinfra/templates/` — jinja2 templates for systemd units, sshd config, etc.
- `scripts/` — operator wrappers: `deploy.sh`, `new-tenant.sh`, `provision-tenant.sh`, `operator-bootstrap-age.sh`, `operator-set-secret.sh`, `offboard-tenant.sh`
- `specs/` — design specs (SPEC-001 through SPEC-020)
- `docs/` — operator-facing documentation (this directory)

## Status

289 / 289 tests passing. Phase-1 build complete (Steps 1 through 7d) + SPEC-021 canonical agent-setup hardening (2026-05-31 outage fixes) + SPEC-001 v1.2 multi-concierge per tenant (agent.concierges list + persona-suffixed watchdogs) + SPEC-001 v1.3 optional git-backed concierge workspace (workspace_repo → clone-if-absent; claudette enabled). See [specs/](specs/) for the per-step contracts.

## License

TODO — to be decided with {{OPERATOR}} (MIT vs proprietary). Assume "all rights reserved" until this is set.
