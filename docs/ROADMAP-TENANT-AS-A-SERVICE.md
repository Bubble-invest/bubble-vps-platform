# Tenant-as-a-Service — Roadmap

**Status:** Planning. Not yet started.
**Owner:** Rick (R&D) — eventually shared with Tony (CEO) once execution starts.
**Source conversation:** Telegram msgs 2782 → 2797 (2026-05-21 afternoon, {{OPERATOR}} + Rick).
**Origin:** {{OPERATOR}} realized that `bubble-vps-platform` already provisions tenants but stops at "concierge agent only". To onboard a real client (or even a second internal tenant like {{OPERATOR_2}}'s instance), we need the full stack: concierge + framework agentique éclos (bubble-ops-loop) + bridge between them.

---

## Vision

> "On peut onboarder quelqu'un avec une solution de serveur + agent framework d'un seul coup." — {{OPERATOR}}, msg 2780

**One command** → a fresh client has:

1. **A dedicated VPS** (Hetzner CX22/CX33 or equivalent), hardened to our standard
2. **A personalized concierge agent** (Karl, Sandra, etc. — chosen by the client; same DNA as Rick but tenant-specific identity)
3. **The bubble-ops-loop framework** installed and ready (so the client can éclore their own Maya, Ben, Miranda, Eliot...)
4. **Secure remote access** for Bubble Invest (us) to intervene if needed — without seeing the client's secrets or conversations
5. **Backup + disaster recovery** activated from day 1
6. **The concierge talks to its owner on Telegram** (the client's personal bot) — and ONLY to its owner

This is the architecture that transforms `bubble-ops-loop` from "our internal tool" into "our product offer". Probably the single most important strategic capability we ship in Q3-Q4 2026.

---

## Architecture: 2 layers per tenant

Each tenant VPS hosts **2 distinct agent layers** that see each other but have different cycles:

### Layer 1 — The concierge (operator-managed, stable)

- **Provisioned by**: pyinfra (`bubble-vps-platform/deploy.py`)
- **Lifecycle**: low-churn (rare changes after install)
- **Role**: observability, monitoring, coordination, escalation, housekeeping. The "concierge of the building".
- **Concrete behavior**:
  - "Maya hasn't ticked in 2h, I'll go check" → does `journalctl` + restart, summarizes to operator on Telegram
  - "Miranda has had a critical gate pending for 6h" → escalation visible on Telegram
  - "Disk at 85%, I'll clean old Restic logs" → housekeeping (with gate if non-trivial)
  - "Restic backup didn't fire this morning, here's why" → diagnostic + recovery plan
- **Has access to**: SSH + sudo on its own VPS (its home). NOT to other tenants. NOT to the depts' Telegram bots.
- **Talks to**: only its owner (the tenant's primary contact). One concierge per tenant, one Telegram bot per concierge.

### Layer 2 — The agentic cabinet (agent-managed, high-churn)

- **Provisioned by**: `scripts/bootstrap-dept.sh` (conversational éclosion)
- **Lifecycle**: high-churn (created / cancelled / retired as the tenant's needs evolve)
- **Role**: actual business work. Each dept has a specialty (prospection, fund management, content, security, etc.)
- **Concrete behavior**: drives its own 7-step éclosion via the `department-onboarding-guide` SKILL, then runs its 4-layer OODA loop in production
- **Has access to**: only what its `gate_policies` authorize. Cannot touch the VPS infrastructure.
- **Talks to**: its owner via its dedicated Telegram bot (one per dept)

### The bridge between layers

- The concierge **can read** the depts' STATE.yaml, journalctl, heartbeat files, queue depths
- The concierge **can act on behalf of the operator** but with `gate_policies` controlling what's autonomous vs gated:
  - `restart_stuck_dept` → `auto_with_veto_window` (act, operator has window to revert)
  - `cancel_eclosion` of a dept → `manual_required` (always asks for confirmation)
  - `housekeeping_disk_cleanup` → `auto_if_policy_passed` (acts if safe rules satisfied)
- The depts **cannot read** the concierge's mailbox (operator-private conversations stay private)

---

## Gap analysis — what's already there vs what's missing

### ✅ Already present (built before today)

| Capability | Location | Notes |
|---|---|---|
| Multi-tenant pyinfra deploy | `bubble-vps-platform/{deploy,inventory}.py` | `TENANT=<name> ./deploy.sh` works |
| Tenant config schema | `bubble-vps-data/tenants/<name>/tenant.yaml` | SPEC-001 v1.0, validated |
| Hardening task | `pyinfra/tasks/hardening/linux.py` | UFW, fail2ban, sshd, unattended-upgrades, swap, Hetzner FW, **OS sandbox (Layer B — bwrap jail for all agents, `_sandbox.py`, 2026-06-02)** |
| Secrets layer | `pyinfra/tasks/secrets/` | SOPS+age, per-tenant keys, never plaintext |
| Agent install task | `pyinfra/tasks/agent/` | Claude Code + Bun + persona sync + systemd |
| Persona structure | `bubble-vps-data/tenants/<name>/persona/<name>/{CLAUDE.md, agent-memory/, skills/, workspace/}` | one persona = morty (the concierge prototype) |
| Tailscale join | `pyinfra/tasks/access/tailscale.py` | tenant joins our tailnet |
| Security audit cron | `pyinfra/tasks/access/security_audit.py` | weekly report |
| Telegram watchdog | `pyinfra/tasks/access/telegram_watchdog.py` | alerts on stale heartbeat |
| Backup scripts (this morning) | `bubble-ops-loop/scripts/{backup-age-key, morty-restic-setup, morty-security-audit}.sh` | shipped 2026-05-21, awaiting operator activation |
| Onboarding playbook | `bubble-vps-platform/docs/ONBOARDING.md` | per-client setup |
| Offboarding playbook | `bubble-vps-platform/docs/OFFBOARDING.md` | shutdown |
| `new-tenant.sh` operator script | `bubble-vps-platform/scripts/new-tenant.sh` | scaffolds a new tenant directory |

### 🔧 Missing — the 5 sprints

| Sprint | Capability | Estimate | Why |
|---|---|---|---|
| 1 | **Persona templating** — make `morty` a reusable concierge template that gets parameterized at tenant creation (`{owner_name, owner_telegram, concierge_name, accent/voice}`) | ~3h | Today morty's CLAUDE.md hardcodes "{{OPERATOR}}" and "Lab" everywhere; a new tenant would inherit {{OPERATOR}}-specific copy |
| 2 | **bubble-ops-loop as a deployable dependency** — package the bubble-ops-loop skill+scripts so a fresh tenant gets it installed (currently the framework lives in Rick's local workspace, not yet shipped to a tenant box) | ~4h | The framework code needs to land on the tenant VPS so the concierge can use `bootstrap-dept.sh` for the client |
| 3 | **Concierge skills + gate policies** — give the concierge the skills (a `dept-supervisor` skill that wraps `journalctl`, systemctl status, gate-policy-aware autonomy on restart/cleanup actions) and the `gate_policies` template | ~3h | Today the concierge has notion-reader, telegram-reporter, scheduled-task-creation, etc. — but no skill to OBSERVE / ACT on bubble-ops-loop depts |
| 4 | **Bubble remote access** — Bubble Invest (us) gets SSH+sudo on every tenant VPS without seeing tenant's secrets, via a `bubble_admin_keys[]` block in tenant.yaml that pyinfra wires into `/root/.ssh/authorized_keys`. With audit log so the tenant can see when we connect | ~2h | Today only the tenant's own keys are deployed; we need a clean way to intervene without ad-hoc SSH key additions |
| 5 | **End-to-end test "fresh tenant Marie"** — a new tenant `client-marie` is created via `new-tenant.sh --name=marie --owner-name="Marie Dupont" --owner-telegram=12345 --concierge-name="Sandra"`. Validate that Sandra speaks to Marie on Telegram, that Marie can ask Sandra "éclôs-moi un dept Maya", and that the éclosion goes through cleanly | ~2h | The integration sentinel for the whole capability |

**Total estimate: ~14h of focused work.** Spread over 3-4 days at a comfortable pace.

---

## Decisions {{OPERATOR}} validated (msg 2794, 2796)

1. **One concierge per tenant, customizable** ("une copie de toi en gros mais personnalisable. C'est assez libre")
2. **Concierge talks only to its owner** — no cross-tenant visibility. Bubble has elevated access when managing the server.
3. **Concierge can create/modify/delete depts** for the operator, with `gate_policies` to prevent mistakes
4. **Bubble (us) has remote access** from the central management server, secured maximally
5. **One VPS per tenant** (no shared mutualized VPS)

---

## Doctrine constraints (must hold)

- **Control plane ≠ data plane**: the concierge (control) can OBSERVE and ACT on depts (data) but cannot READ their Telegram conversations (different bots, different `TELEGRAM_BOT_TOKEN` per dept, isolated by systemd EnvironmentFile)
- **5-mode autonomy enum** applies to concierge gates too (`current_mode: manual_required` default, `eligible_future_modes` choosable from the 5 official)
- **No shadow_autonomy / full_autonomy** as future_eligible_modes (doctrine fix from PR #4)
- **Karpathy + bubble-ops discipline** (5 principles) in the concierge's CLAUDE.md too — same scope-creep / surgical-changes / stay-in-périmètre rules apply
- **Bureau-de-Cadre voice** for all operator-facing copy (concierge, depts, console)

---

## Open questions to resolve before Sprint 1

- **GitHub org for tenants' repos**: `{{GITHUB_OWNER}}/` ({{OPERATOR}}'s personal) or a future Bubble Invest org? Impacts the dept repo creation flow in `bootstrap-dept.sh`.
- **DNS for client-facing console**: today `tailscale-only` access. Do clients need a public URL (`marie.cabinets.bubbleinvest.com`) or stay Tailscale-only?
- **Pricing model for paying clients**: bundled VPS + concierge + framework? À la carte? Impacts the `tenant.yaml` schema (`billing` block?).
- **Concierge model choice**: Opus 4.7 (premium, costs more) or Sonnet 4.5 (cheaper but less reasoning)? Per-tenant choice in `tenant.yaml::agent.llm.model`?
- **Off-site backup target**: Hetzner Storage Box (€3.81/mo, same vendor as Hetzner VPS) or Backblaze B2 (~€1/mo, vendor-isolated)? Per-tenant or shared?

---

## Sequencing (when we attack it)

**Pre-requisites** (already done):
- ✅ Refonte granulaire 7 étapes (today)
- ✅ Lifecycle scripts (today)
- ✅ Backup + DR documented (today)

**Recommended order**:
1. Maya first onboarding (validates the éclosion flow on `bubble-internal`, semi-clean) — Maya might be tomorrow or whenever {{OPERATOR}} is ready
2. Hardening sprint P1 from msg 2783 (PermitRootLogin, rotation drill) — 1.5h
3. **Tenant-as-a-Service sprints 1-5** — ~14h, ideally start AFTER Maya proved the éclosion flow works in real life
4. Onboard a SECOND tenant (`bubble-internal-{{OPERATOR_2}}` or a real `client-xxx`) as the integration test — proves Sprint 5 wasn't lying

---

## Risk: scope creep

{{OPERATOR}} and Rick must resist the temptation to make Tenant-as-a-Service "complete" before shipping. Ship Sprint 1+2+5 first (a tenant can be created and Sandra can talk to Marie), gate Sprint 3+4 behind real usage signals.

The Karpathy principle #2 ("Simplicité d'abord") applies recursively to this roadmap itself.
