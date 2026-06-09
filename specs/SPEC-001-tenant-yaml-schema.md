# SPEC-001 — Tenant YAML schema

**Status:** Draft v1.3
**Author:** Lab (rnd)
**Date:** 2026-05-08 (v1.2 multi-concierge: 2026-05-31; v1.3 git-backed workspace: 2026-05-31)
**Reviewed by:** _pending Joris approval_

---

## Changelog

- **v1.3 (2026-05-31)** — OPTIONAL GIT-BACKED concierge workspace. A concierge
  may set `workspace_repo` (a git URL) + optional `workspace_branch` (default
  `main`) to declare that its WORKDIR is its OWN git repo rather than a curated
  `persona/<name>/workspace/` tree in the data repo. The deploy then CLONES
  that repo into `/home/claude/agents/<name>` (clone-if-absent; no destructive
  sync) instead of mirroring a data-repo workspace tree. Mutually exclusive
  with an on-disk `persona/<name>/workspace/` tree (ambiguity rejected at parse
  time). Motivated by claudette, whose workspace IS
  `bubble-claudette-workspace` (her own repo with her own `.sops.yaml` +
  secrets, demos, ~1GB of files) — mirroring it into the data repo would
  duplicate her repo, nest her secrets in ours, bloat the data repo, and break
  her git workflow. Also future-proofs Tenant-as-a-Service (a client concierge
  typically owns its repo too). See "Git-backed vs synced workspace" below.
- **v1.2 (2026-05-31)** — MULTI-CONCIERGE per tenant. `agent.persona` (a single
  object) → `agent.concierges` (a LIST). A tenant may host MULTIPLE trusted
  general-purpose concierges (Joris msg 3376). Each concierge is self-contained
  (name + persona_dir + channels + llm + optional systemd). The legacy single
  `agent.persona` form is still accepted as a BACK-COMPAT SHIM (normalized to a
  one-element list) so existing client tenants don't break. See
  "Agent configuration" below + SPEC-021 inv#6 (concierge UNPREFIXED workdir).
- **v1.1** — `schema_version` field REQUIRED; `secrets:` block (SPEC-006).
- **v1.0** — initial schema.

---

## Purpose

Define the schema for `tenants/<name>/tenant.yaml` — the SINGLE source of truth for a tenant's non-secret configuration. This file lives in the data repo (private) and is read by the pyinfra inventory at deploy time.

**Design constraint:** must be sufficient to fully describe a tenant's deployment, EXCEPT for secrets (which live in `secrets.sops.env`) and persona files (which live in `persona/`).

---

## Schema (v1)

```yaml
# tenants/<name>/tenant.yaml

# ─── Tenant identity ────────────────────────────────────────────────
tenant_name: bubble-internal               # REQUIRED, lowercase-kebab. Matches dir name.
tenant_type: internal                       # REQUIRED. Enum: internal | client
display_name: Bubble Internal               # REQUIRED. Human-readable.
contact:                                    # REQUIRED for type=client, optional for internal
  primary_email: joris@bubbleinvest.fr
  primary_telegram_user_id: "6532205130"

# ─── Infrastructure ─────────────────────────────────────────────────
host:
  ip: 178.105.77.178                        # REQUIRED. IPv4.
  hostname: joris-cx33                      # REQUIRED. Used for SSH alias generation.
  ssh_user: claude                          # REQUIRED. The user pyinfra connects as.
  ssh_port: 22                              # OPTIONAL. Default 22.
  os_family: linux                          # REQUIRED. Enum: linux | macos
  os_distro: ubuntu                         # REQUIRED for linux. Enum: ubuntu | debian
  os_version: "24.04"                       # REQUIRED for linux.
  provider: hetzner                         # REQUIRED. Enum: hetzner | aws | gcp | byo
  provider_server_id: "129474747"           # OPTIONAL. For Hetzner-API-driven cleanup.
  region: fsn1-dc14                         # OPTIONAL. Provider-specific.

# ─── Hardening profile ─────────────────────────────────────────────
hardening:
  ufw:
    enabled: true                           # REQUIRED. Whether UFW manages firewall.
    allow_ssh_from: any                     # REQUIRED. Enum: any | <CIDR list>
  fail2ban:
    enabled: true
    sshd_jail: aggressive                   # Enum: default | aggressive
    bans:
      maxretry: 5
      findtime_minutes: 10
      bantime_hours: 1
  sshd:
    permit_root_login: "no"                 # Enum: yes | no | prohibit-password
    password_authentication: "no"
    max_auth_tries: 3
  unattended_upgrades:
    enabled: true
    auto_reboot_time: "04:00"               # UTC
  swap:
    enabled: true
    size_gb: 2
    swappiness: 10
  hetzner_cloud_firewall:
    enabled: true
    firewall_id: "10938002"                 # OPTIONAL but recommended.

# ─── Agent configuration ───────────────────────────────────────────
agent:
  install:
    claude_code: true                       # REQUIRED. Install Claude Code.
    nodejs_version: "22"                    # OPTIONAL. Default 22.
    bun: true                               # REQUIRED for Telegram plugin.

  plugins:
    - telegram@claude-plugins-official      # REQUIRED enabled plugins (box-level).

  # MULTI-CONCIERGE (v1.2). `concierges` is a LIST — a tenant box may host
  # several trusted general-purpose concierges. Each is self-contained. The
  # deploy loops per concierge → one claude-agent-<name>.service + settings +
  # trust-seed (UNPREFIXED workdir /home/claude/agents/<name>, SPEC-021 inv#6)
  # + its own Telegram channel + its own persona-suffixed watchdog
  # (telegram-watchdog-<name>.*). The PRIMARY (first) concierge keeps the
  # historical runtime env path /run/claude-agent/env; additional concierges
  # decrypt into /run/claude-agent-<name>/env.
  concierges:
    - name: ricky                           # REQUIRED. Used in service/channel/watchdog names. UNIQUE per tenant.
      persona_dir: persona/ricky            # REQUIRED. Relative path inside this tenant dir. Must EXIST (parse-time check).
      # persona_dir must contain at minimum CLAUDE.md. Optional: agent-memory/,
      # skills/ (GLOBAL — additive), workspace/.
      # workspace_repo: https://github.com/org/ricky-workspace.git  # OPTIONAL (v1.3). Git-backed workdir.
      # workspace_branch: main                # OPTIONAL (v1.3). Default "main". Only meaningful with workspace_repo.
      # ↑ If workspace_repo is set, the deploy CLONES it into the workdir
      #   /home/claude/agents/<name> directly (clone-if-absent) and does NOT
      #   sync a persona/<name>/workspace/ tree. Set EITHER workspace_repo OR a
      #   persona workspace/ tree — never both (see "Git-backed vs synced").
      channels:
        telegram:
          enabled: true
          bot_token_secret_ref: TELEGRAM_BOT_TOKEN   # REQUIRED when enabled. Key name in secrets.sops.env.
          allowed_user_ids:                          # REQUIRED non-empty when enabled.
            - "6532205130"
      llm:
        provider: anthropic                 # REQUIRED. Enum: openrouter | anthropic
        auth_mode: claude_code_subscription # api_key | claude_code_subscription
        model: "opus[1m]"                   # REQUIRED. Model alias/slug (SPEC-021 inv#1).
      systemd:                              # OPTIONAL per-concierge overrides.
        restart: on-failure
        restart_sec: 10
        nofile_limit: 65536
    # - name: sandra                        # A SECOND concierge (Tenant-as-a-Service).
    #   persona_dir: persona/sandra
    #   channels: { telegram: { enabled: true, bot_token_secret_ref: SANDRA_TELEGRAM_BOT_TOKEN, allowed_user_ids: ["..."] } }
    #   llm: { provider: anthropic, auth_mode: claude_code_subscription, model: "opus[1m]" }

  # ─── BACK-COMPAT (legacy single-concierge form) ──────────────────────────
  # The pre-v1.2 single form is STILL ACCEPTED and normalized to a one-element
  # concierges list. Set EITHER agent.concierges OR the legacy block, not both:
  #
  #   agent:
  #     persona: { name: ricky, persona_dir: persona/ricky }
  #     channels: { telegram: {...} }
  #     llm: { provider: ..., model: ... }
  #     systemd: {...}

# ─── Access ────────────────────────────────────────────────────────
access:
  tailscale:
    enabled: true                           # REQUIRED. Our support access.
    authkey_secret_ref: TAILSCALE_AUTHKEY   # REQUIRED. Key name in secrets.sops.env.
    tags:                                   # OPTIONAL. ACL scoping.
      - "tag:tenant"
      - "tag:tenant-bubble-internal"
    accept_routes: false
    advertise_routes: []

  phone_home:
    enabled: true
    dashboard_url_secret_ref: DASHBOARD_INGEST_URL  # OPTIONAL. Where heartbeats go.
    interval_minutes: 5

# ─── Metadata ──────────────────────────────────────────────────────
created_at: "2026-05-06"
provisioned_by: lab
notes: |
  Bubble Invest's internal Tenant #1. Hosts our cloud-side agents
  (Ricky to start). Hardware is a Hetzner CX33 in fsn1-dc14.
```

---

## Git-backed vs synced workspace (v1.3)

A concierge's WORKDIR (`/home/claude/agents/<name>`) is sourced one of two ways:

| Model | Trigger | How the deploy populates the workdir | Canonical source |
|-------|---------|--------------------------------------|------------------|
| **Synced** (default — e.g. MORTY) | NO `workspace_repo` | `files.sync(delete=True)` mirrors the data-repo `persona/<name>/workspace/` tree into `/home/claude/agents/<name>/workspace/` | the data repo |
| **Git-backed** (e.g. CLAUDETTE) | `workspace_repo` set | `test -d <workdir>/.git \|\| git clone --branch <workspace_branch> <workspace_repo> <workdir>` — clone-if-absent into the workdir DIRECTLY (files at top level, no `workspace/` subdir) | the concierge's OWN git repo |

**Rationale.** Some concierges' workdirs are already their own git repos with
their own secrets, `.sops.yaml`, demos and large assets (claudette →
`bubble-claudette-workspace`, 1410 files, ~1GB). Copying such a repo into our
data-repo `persona/` would (a) duplicate the repo, (b) nest the concierge's
secrets inside ours, (c) bloat the data repo, and (d) break the concierge's own
git workflow. The git-backed model keeps the concierge's repo as the single
source of truth and merely *references* it.

**Deploy semantics for git-backed concierges:**

- **Clone-if-absent only.** The deploy GUARANTEES the clone EXISTS. It does NOT
  `git pull`/`git reset` on subsequent runs — that could clobber the agent's
  uncommitted runtime work. Keeping the checkout current is the agent's OWN
  `git pull` responsibility, not the deploy's.
- **No destructive sync.** `files.sync(delete=True)` MUST NEVER touch a
  git-backed concierge's workdir (the data-loss risk this model avoids).
- **Dangling-symlink aware.** If the workdir path is a dangling symlink (target
  gone), the deploy hard-errors rather than cloning over it (mirrors
  `bubble-ops-loop/scripts/deploy-to-morty.sh`).
- **Identity + memory unchanged.** Both models STILL deploy `CLAUDE.md →
  ~/.claude/agents/<name>.md` and `agent-memory/ → ~/.claude/agent-memory/<name>/`
  from the data-repo `persona/<name>/` — only the WORKSPACE step branches. A
  git-backed concierge's `persona_dir` therefore contains `CLAUDE.md` (+
  optional `agent-memory/`, `skills/`) but NO `workspace/` subdir.

---

## Validation rules

A tenant.yaml is valid iff:

1. **Required fields present** — see REQUIRED markers above.
2. **Enums match** — `tenant_type`, `os_family`, `os_distro`, `provider`, `permit_root_login`, `llm.provider` only accept listed values.
3. **`tenant_name` matches the directory name** — `tenants/foo/tenant.yaml` MUST have `tenant_name: foo`. Schema validator enforces.
4. **Secret refs are uppercase** — anything ending in `_secret_ref` must be UPPER_SNAKE_CASE.
5. **All `_secret_ref` values exist as keys in the tenant's `secrets.sops.env`** — checked at deploy time, not parse time.
6. **Every concierge's `persona_dir` exists relative to the tenant directory** — checked at parse time (one check per concierge in the list).
7. **`host.ip` is a valid IPv4** — basic regex check.
8. **`tenant_type: client` requires non-empty `contact.primary_email`.**
9. **`agent.concierges` is a non-empty list** — ≥1 concierge required (v1.2).
10. **Concierge `name`s are UNIQUE within a tenant** — names drive the systemd service / Telegram channel / runtime-env / watchdog unit names; a duplicate would collide on all of them (v1.2).
11. **EITHER `agent.concierges` OR the legacy `agent.persona` form — not both** — setting both is ambiguous and rejected (v1.2 back-compat shim).
12. **A concierge sets EITHER `workspace_repo` OR a data-repo `persona/<name>/workspace/` tree — not both** (v1.3). `workspace_repo` (when present) must be a non-empty string git URL; `workspace_branch` (when present) must be a non-empty string and defaults to `main`. The both-set check is enforced at parse time when the tenant directory is known (it inspects the on-disk persona dir for a `workspace/` subdir).

Validation lives in `pyinfra/lib/tenant_loader.py`. Errors fail the deploy with a clear message before any SSH work begins.

---

## Versioning

Schema is versioned by directory naming + a top-level `schema_version: 1` field (REQUIRED in v1.1+, OPTIONAL in v1.0 for backwards-compat). Major bumps require a migration script in `scripts/migrate-tenant-schema.sh`.

---

## Out of scope for v1

These will land in v2+:
- ~~Multiple agents per tenant (v1 = one agent per tenant)~~ — **DELIVERED in v1.2** as `agent.concierges` (multiple CONCIERGES per tenant). Note: per-concierge SECRETS isolation (each concierge's env file exposing ITS bot token under the plugin's expected `TELEGRAM_BOT_TOKEN` key) is a remaining secrets-layer follow-up — see SPEC-006 / the tenant.yaml claudette TODO.
- Per-environment overlays (dev/staging/prod for the same tenant)
- Pre-deploy hooks (custom shell to run before pyinfra)
- Post-deploy hooks (custom shell to run after pyinfra)
- Backup/snapshot policy declarations

---

## Reference: example minimal tenant.yaml

For a generic client setup, the minimal viable file looks like:

```yaml
tenant_name: acme-corp
tenant_type: client
display_name: Acme Corp
contact:
  primary_email: ops@acme.example.com
  primary_telegram_user_id: "1234567"
host:
  ip: 1.2.3.4
  hostname: acme-corp-vps
  ssh_user: claude
  os_family: linux
  os_distro: ubuntu
  os_version: "24.04"
  provider: hetzner
hardening: {ufw: {enabled: true, allow_ssh_from: any}, fail2ban: {enabled: true}, sshd: {permit_root_login: "no", password_authentication: "no"}, unattended_upgrades: {enabled: true}, swap: {enabled: true, size_gb: 2}}
agent:
  install: {claude_code: true, bun: true}
  persona: {name: acme-bot, persona_dir: persona/acme-bot}
  channels:
    telegram: {enabled: true, bot_token_secret_ref: TELEGRAM_BOT_TOKEN, allowed_user_ids: ["1234567"]}
  llm: {provider: openrouter, base_url: "https://openrouter.ai/api", api_key_secret_ref: OPENROUTER_API_KEY, model: "deepseek/deepseek-v4-pro"}
  plugins: [telegram@claude-plugins-official]
access:
  tailscale: {enabled: true, authkey_secret_ref: TAILSCALE_AUTHKEY}
  phone_home: {enabled: true, interval_minutes: 5}
```

This is the form `scripts/new-tenant.sh` will generate.
