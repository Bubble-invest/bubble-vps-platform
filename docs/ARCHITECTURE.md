# ARCHITECTURE — Bubble VPS Platform

Executive summary of how the platform is composed. This is the orientation doc; detailed contracts live in the SPEC-* docs under [`../specs/`](../specs/) and the master vision lives in `proposals/end-state-vision.md`.

For the three-architectures comparison (Anthropic-cloud / Linux-VPS / local-Mac) that frames why we picked VPS for Phase 1, see `proposals/three-architectures-reference.md`.

---

## Two-repo split

The platform is split into two git repositories that live as siblings on the operator Mac:

- **`bubble-vps-platform`** (this repo) — sellable / open-sourceable. Contains pyinfra task modules, jinja2 templates, operator scripts, the tenant config loader, and the docs you are reading. No customer names, no API keys, no real persona data. Every file in this repo could be public tomorrow.
- **`bubble-vps-data`** (private companion) — never shared. Contains `.sops.yaml`, `tenants/<name>/{tenant.yaml, secrets.sops.env, persona/, README.md}` for each tenant. SOPS recipient lists ensure only the right age key can decrypt the right tenant's secrets.

The platform code reads tenant configs from the data repo at deploy time (path resolved via `BUBBLE_DATA_REPO` env var, defaulting to `../bubble-vps-data`). Tenant config schema is defined in [SPEC-001](../specs/SPEC-001-tenant-yaml-schema.md). The inventory + deploy entrypoints are defined in [SPEC-002](../specs/SPEC-002-inventory-and-deploy.md).

---

## Layers (top-down)

The deploy entrypoint orchestrates five layers per tenant. Each layer is a directory under `pyinfra/tasks/` with its own `deploy.py` and submodules. Each is independently idempotent.

### Hardening layer ([SPEC-005](../specs/SPEC-005-linux-hardening.md))

- sshd config drop-in, validated with `sshd -t -f` before reload
- UFW (default deny incoming, allow outgoing, rate-limit 22/tcp — see [SPEC-004](../specs/SPEC-004-ssh-rate-limit-policy.md))
- fail2ban (sshd jail aggressive + recidive jail)
- unattended-upgrades (security-only origins, auto-reboot 04:00 UTC)
- Swap (2 GB swapfile, vm.swappiness=10)
- chrony (NTP)

The drift test ([RUNBOOK.md](RUNBOOK.md) §"How to verify the hardening playbook is healthy") is the litmus: applying this layer twice against an already-hardened host must report `Changed: 0`.

### Secrets layer ([SPEC-006](../specs/SPEC-006-secrets-sops-age.md), [SPEC-008](../specs/SPEC-008-secrets-deploy-second-half.md))

- SOPS + age binaries installed on the box
- Box-side age keypair generated (`/etc/age/key.txt`, root:root, 0400)
- Encrypted secrets file shipped to `/etc/bubble/secrets.sops.env` (root:root, 0440)
- Test-decrypt + required-key validation, with the **hard rule** that no plaintext value ever reaches stdout/stderr/transcript
- Runtime decryption to tmpfs at `/run/claude-agent/env` via systemd ExecStartPre (RAM only, never touches disk)
- Restart-on-change hook ([SPEC-012](../specs/SPEC-012-secrets-restart-on-change.md)) — `files.put` triggers a service restart only when the encrypted blob's hash actually changes

### Agent layer ([SPEC-007](../specs/SPEC-007-agent-install.md), [SPEC-009](../specs/SPEC-009-step4-addendum-claude-code-subscription.md), [SPEC-010](../specs/SPEC-010-step5a-morty-persona.md))

- Node + bun + Claude Code installed
- systemd unit `claude-agent-<persona>.service` with EnvironmentFile pointing at `/run/claude-agent/env`
- Persona files (CLAUDE.md, agent-memory, skills) rsynced from `bubble-vps-data/tenants/<name>/persona/<persona>/`
- Telegram plugin + MCP servers from `tenant.yaml`'s plugin block
- Claude Code subscription auth via `CLAUDE_CODE_OAUTH_TOKEN` (from secrets), avoids the per-token billing path

### Access layer ([SPEC-011](../specs/SPEC-011-tailscale.md), [SPEC-013](../specs/SPEC-013-telegram-recovery-watchdog.md))

- Tailscale agent installed + joined with `tag:bubble-tenant`, no advertised routes, no accepted routes
- Telegram-watchdog cron (5 min cadence) that detects stuck `bun` polling and kicks the service
- Public IP path remains as a fallback (operator can SSH via UFW-rate-limited port 22 if Tailscale is broken)

### Monitoring layer ([SPEC-014](../specs/SPEC-014-cloud-security-cron.md), [SPEC-015](../specs/SPEC-015-phone-home-dashboard.md), [SPEC-020](../specs/SPEC-020-cloud-wiki-sync.md))

- Phone-home daemon — POSTs telemetry (no data content; only metadata: service-up, disk %, restart count, claude version) every 5 min to the central dashboard
- Central dashboard on `joris-cx33` — Tailscale-only exposure, SQLite-backed, single page table view of all tenants
- Daily security-audit cron at 09:00 UTC — reports to a Telegram chat with auth/secrets/agent/CVE/disk/transcripts/version/firewall checks
- Cloud wiki sync — keeps the agent's shared-wiki in sync from a central source so on-box agents share knowledge with operator-side agents

---

## Trust model

Two roots of trust, two SOPS recipients per tenant:

- **Operator Mac** — holds the master age private key (`~/.config/sops/age/keys.txt`). This is the recipient that lets the operator decrypt + edit the secrets file. Compromise = all tenants compromised. Mitigated by macOS Keychain protection on the file + FileVault on the disk. See [SECURITY.md](SECURITY.md) §"Threat model".
- **Tenant box** — holds its own age private key (`/etc/age/key.txt`). This is the recipient that lets the box's systemd ExecStartPre decrypt at service start. Compromise = that one tenant's secrets at risk; the operator can still decrypt + rotate.

The `.sops.yaml` for each tenant lists both pubkeys as recipients. Re-encryption with `sops updatekeys` is what propagates membership changes.

The two-recipient design is what enables [offboarding handoff](OFFBOARDING.md) — we can remove the operator pubkey from `.sops.yaml`, run `sops updatekeys`, and now only the box can decrypt. We have provably lost access.

For the per-process trust model on the box (which UID can read which file), see [SPEC-006](../specs/SPEC-006-secrets-sops-age.md) §"Threat model".

---

## Network model

- **Tailscale mesh** — primary path for ops access. All tenant boxes join our tailnet with `tag:bubble-tenant`. Operator's Mac is the tagged owner. SSH via `ssh <tenant>-vps` (MagicDNS resolves it).
- **Public IP** — fallback path. UFW allows port 22 from anywhere with rate-limit (6 conns / 30s / IP per [SPEC-004](../specs/SPEC-004-ssh-rate-limit-policy.md)). Used when Tailscale is broken on operator side (rare) or during the first deploy before the box has joined the tailnet.
- **Hetzner Cloud Firewall** — hypervisor-level layer attached at provisioning ([SPEC-017](../specs/SPEC-017-hetzner-provisioning.md)). Allows port 22 + ICMP from internet; everything else denied at the hypervisor before traffic reaches the VM.
- **Outbound** — unrestricted (the agent needs to reach Anthropic, Telegram, npm, GitHub, etc.). No egress filtering in v1.

---

## Visibility model

- **Phone-home daemon (per tenant)** — every 5 min POST to dashboard with telemetry only. Schema in [SPEC-015](../specs/SPEC-015-phone-home-dashboard.md) §"Telemetry contract".
- **Central dashboard (one host, joris-cx33 for now)** — single web page with all tenants + click-through detail views. Tailscale-only exposure, never bound to 0.0.0.0.
- **Security-audit cron (per tenant)** — daily 09:00 UTC report to a Telegram chat. 8 sections, 100-point score. See [SPEC-014](../specs/SPEC-014-cloud-security-cron.md).
- **Telegram-watchdog cron (per tenant)** — 5 min cadence, posts only on broken-state recovery (silent on healthy state).

No data content ever leaves the box via these paths — only metadata, counts, and timestamps. The "no transcripts to dashboard" boundary is intentional (per [SPEC-015](../specs/SPEC-015-phone-home-dashboard.md)).

---

## What we don't build (deliberately)

Per `proposals/end-state-vision.md` §"What we DON'T have":

- No web admin panel (everything via CLI / dashboard — we are a small team, not a SaaS company)
- No multi-region failover (each tenant has one box; if it dies, restore from snapshot)
- No automated billing (Stripe / manual invoicing, outside this system)
- No self-serve API for clients (onboarding is human-in-the-loop per [ONBOARDING.md](ONBOARDING.md))
- No mobile app (dashboard is web, Telegram is the interaction channel)
- No CI/CD for deploys (operator runs `pyinfra deploy` from their Mac; later: GitHub Actions, but not Phase 1)
- No multi-cloud (Hetzner only for Phase 1; AWS/GCP would mean Terraform on top of pyinfra)

These are intentional simplifications. We add them when volume justifies the complexity. See `proposals/end-state-vision.md` §"What success looks like (the litmus test)" for the seven Phase-1 acceptance criteria.
