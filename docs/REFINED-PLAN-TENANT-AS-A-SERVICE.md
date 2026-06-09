# Refined Plan — Tenant-as-a-Service

**Status:** Refinement of `ROADMAP-TENANT-AS-A-SERVICE.md` (committed 2026-05-21). Read that doc first for context; this doc adds depth, not breadth.
**Author:** Rick (R&D), 2026-05-21 evening session.
**Audience:** {{OPERATOR}} (for validation of the 5 open questions + the re-sequencing) + future sub-agents who pick up the chantier.
**Confidence labels per section:** [HIGH] = grounded in code I read, [MED] = grounded in code + reasonable extrapolation, [LOW] = opinion or TBD-with-{{OPERATOR}}.

---

## 1. Executive summary

Four findings dominate this refinement:

1. **The roadmap's 14h estimate is optimistic by ~50%.** Honest range: **20–24h**. The gap is hidden test-and-doc work (≥6 existing tests need updating per sprint, each touching ≥2 files), the "dashboard host" hardcode in `pyinfra/tasks/monitoring/dashboard.py:59`, and the "JORIS_TG_USER_ID" naming convention in 3 j2 templates that bleeds into Bubble Cabinet too.

2. **Sprint 2 (bubble-ops-loop deployable) is structurally the biggest** because the framework currently lives in `~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/` and assumes (a) operator-Mac paths, (b) `vdk888` as GitHub org (3 hardcoded references), (c) `/home/claude/agents/morty/` paths in `deploy-to-morty.sh`. A real packaging pass is closer to 6–8h than 4h.

3. **The roadmap is missing a Sprint 0 — "schema additions"** that nearly every other sprint depends on. Adding `tenant.yaml::bubble_admin_keys[]`, `tenant.yaml::owner.{display_name,locale,voice_style}`, `tenant.yaml::access.hosts_dashboard` and `tenant.yaml::billing` IS the unblocking work for Sprints 1, 3, 4 — and it changes `lib/tenant_loader.py` + 18 tests. Doing it once upfront saves rework later.

4. **The "Day-2 operations" gap is real and the roadmap doesn't cover it.** Specifically: framework-upgrade-to-live-tenants (no mechanism), tenant-driven decommission (we have offboard-tenant.sh but no self-service path), and SLA model (zero promises currently). Without these three, T-a-a-S is not actually sellable — you can onboard but you cannot operate a fleet.

The strongest re-sequencing argument: **Sprint 0 (schema) + Sprint 1 (persona templating) + Sprint 5 (E2E test) is the true MVP**. Sprints 2-4 can ship after a real tenant is paying.

---

## 2. Per-sprint deep dive

### Sprint 0 — Schema additions (NEW, recommended PRE-requisite) [HIGH confidence]

The original roadmap has no Sprint 0. I'm adding one because every downstream sprint requires schema changes; doing them iteratively means rewriting validation + tests N times.

**Files created/modified:**
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/lib/tenant_loader.py` — add `OwnerConfig`, `BillingConfig`, `BubbleAdminConfig` dataclasses; add `_parse_owner()`, `_parse_billing()`, `_parse_bubble_admin()`; extend `TenantConfig`.
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/specs/SPEC-001-tenant-yaml-schema.md` — bump schema_version to 2; document new blocks.
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/tenant.yaml.j2` — render new placeholders.
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-data/tenants/bubble-internal/tenant.yaml` — add the new blocks (backfill bubble-internal as the canonical example).
- New schema doc: `specs/SPEC-021-tenant-yaml-v2-additions.md`.

**Data shapes added to tenant.yaml:**
```yaml
schema_version: 2  # bumped

owner:
  display_name: "Marie Dupont"     # used by persona render
  locale: "fr"                      # fr|en; drives Bureau-de-Cadre voice
  voice_style: "vouvoiement"        # vouvoiement|tutoiement; concierge voice
  concierge_name: "Sandra"          # used in /etc/systemd/system/claude-agent-sandra.service

billing:
  plan: "starter|pro|enterprise"    # à la carte alternative below
  bundled: true                      # if true: VPS+concierge+framework one price
  monthly_eur: 99                    # informational; not enforced anywhere
  notes: "Marie's plan — 2026-05-21"

bubble_admin_keys:
  # SPEC-TBD: each entry is a Bubble Invest engineer's pubkey + audit metadata
  - name: rick
    pubkey_ssh: "ssh-ed25519 AAAA...rick@bubbleinvest"
    pubkey_added_at: "2026-05-21"
    expires_at: "2026-11-21"   # 6mo default; rotation policy

access:
  hosts_dashboard: false      # was hardcoded to bubble-internal; now schema flag
```

**Helper functions / classes (signatures only — no implementation here):**
- `_parse_owner(d: Optional[dict]) -> OwnerConfig` — validates locale enum, voice_style enum, non-empty concierge_name.
- `_parse_billing(d: Optional[dict]) -> Optional[BillingConfig]` — optional block; if present, validate plan enum.
- `_parse_bubble_admin(d: Optional[dict]) -> list[BubbleAdminKeyConfig]` — list shape; validates pubkey starts with `ssh-ed25519` or `ssh-rsa`, expires_at is ISO date.

**Tests to ship (count: 10):**
1. `test_owner_block_required_for_client_type` — pin: `tenant_type=client` MUST have `owner.concierge_name` non-empty.
2. `test_owner_locale_enum_valid` — fr/en accepted, "de" rejected.
3. `test_voice_style_enum_valid` — vouvoiement/tutoiement accepted.
4. `test_billing_block_optional_for_internal` — internal tenants pass with no billing block.
5. `test_billing_plan_enum_valid` — starter/pro/enterprise.
6. `test_bubble_admin_keys_list_shape` — accepts empty list, rejects non-list.
7. `test_bubble_admin_pubkey_format` — rejects malformed pubkey.
8. `test_bubble_admin_expires_at_iso_format` — rejects "tomorrow", accepts "2026-11-21".
9. `test_schema_version_2_required_when_new_blocks_present` — v1 + owner block → reject.
10. `test_existing_v1_tenants_still_load` — bubble-internal.yaml v1 (no owner block) still parses, with sensible defaults.

**Acceptance criteria:**
- `pytest lib/test_tenant_loader.py` passes with 28 tests (was 18 + 10 new).
- `lib/tenant_loader.py::load_tenant("bubble-internal")` returns the new fields populated from backfilled `bubble-vps-data/tenants/bubble-internal/tenant.yaml`.
- All 17 other test files still pass with `pytest` zero failures.

**Risks + mitigations:**
- *Backward compat with bubble-internal*: mitigate by making the new blocks OPTIONAL when `tenant_type=internal` and `schema_version=1`.
- *Schema sprawl*: bubble_admin_keys feels like it belongs in a separate file (it's operator-side, not tenant-side). I'm keeping it inline for now — moving later is cheap.

**Honest estimate: ~3h** (was: not in roadmap). Includes spec doc + tests.

---

### Sprint 1 — Persona templating [HIGH confidence]

Original estimate: 3h. Refined: **~4h**.

**Files created/modified:**
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/persona-claude-md.j2` (currently a 1-line stub at line 1 — see Read output). Replace with a full templated persona that takes `{owner_display_name, concierge_name, locale, voice_style, telegram_bot_handle}`.
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/persona-workspace-claude-md.j2` (referenced in `new-tenant.sh:242` but I didn't audit it — likely also stub).
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/persona-cwd-claude-md.j2` (NEW — mirror of `bubble-vps-data/tenants/bubble-internal/persona/morty/cwd-CLAUDE.md`, parameterized).
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/scripts/new-tenant.sh` — add `--owner-name`, `--owner-locale`, `--voice-style`, `--concierge-name` flags + render the new templates.
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/agent/_persona.py` — add a render step before the rsync, so the persona dir on the operator Mac is materialized from template at deploy time too (currently the persona dir is assumed pre-existing; for a fresh tenant scaffolded by new-tenant.sh, this gap is hidden because new-tenant.sh renders the file directly).

**Hidden assumptions in existing code that break under templating:**
- `pyinfra/tasks/agent/_persona.py:9` docstring hardcodes `bubble-internal/persona/morty/` as the canonical example. Cosmetic but worth updating.
- `bubble-vps-data/tenants/bubble-internal/persona/morty/CLAUDE.md` lines 12-16 contain "Cloned from Lab on 2026-05-09" and "{{VPS_HOST}}" — these are baked into the bubble-internal-as-Tenant-#1 narrative. The reusable template MUST strip this and let new-tenant.sh inject the new tenant's narrative.
- `cwd-CLAUDE.md` lines 89-95 hardcode `@ContentbubbleClawbot` (Morty's bot). Template MUST inject `{telegram_bot_handle}`.
- `cwd-CLAUDE.md` line 122 references `emit_kanban_item.sh` (Mac-side push). Multi-tenant: drop this line for client tenants; keep it for internal-type tenants.
- `agent-memory/MEMORY.md` is morty-specific (5807-byte file {{OPERATOR}}-flavored). For client tenants, generate a 100-byte stub: "Memory bootstrapped 2026-05-21. Empty — Sandra will fill this as she works."

**Data shapes (jinja2 template variables):**
```
persona-claude-md.j2 receives:
  - tenant_name: str
  - tenant_type: "internal" | "client"
  - owner_display_name: str
  - owner_locale: "fr" | "en"
  - voice_style: "vouvoiement" | "tutoiement"
  - concierge_name: str (e.g. "Sandra")
  - telegram_bot_handle: str (e.g. "bubblecabinet_acme_bot")
  - tailnet_hostname: str (e.g. "acme-cx22" — derived from tenant_name)
  - created_at: ISO date
```

**Helper functions:**
- `lib/persona_render.py::render_persona_files(cfg: TenantConfig, output_dir: Path) -> list[Path]`
  *Renders all 4 persona files from j2 templates into output_dir. Returns list of files written. Used by both new-tenant.sh and pyinfra/_persona.py.*

**Tests to ship (count: 12):**
1. `test_persona_render_fills_owner_display_name`
2. `test_persona_render_fills_concierge_name`
3. `test_persona_render_voice_style_fr_vouvoiement` (string match: "Bonjour Marie" with "vous")
4. `test_persona_render_voice_style_fr_tutoiement` (string match: "Salut Marie" with "tu")
5. `test_persona_render_voice_style_en` (English variant)
6. `test_persona_render_no_morty_string_leaks` — pin: rendered persona for tenant "acme" must NOT contain "morty", "joris", "Lab", "Ricky", "bubble-internal".
7. `test_persona_render_telegram_handle_present`
8. `test_persona_render_tailnet_hostname_present`
9. `test_persona_render_for_internal_tenant_keeps_kanban_emit` (internal tenants preserve `emit_kanban_item.sh` line).
10. `test_persona_render_for_client_tenant_drops_kanban_emit`
11. `test_new_tenant_sh_creates_rendered_persona` (integration — calls new-tenant.sh with all flags, greps the output).
12. `test_idempotent_re_render` — running render_persona_files twice on same output_dir produces identical bytes.

**Existing tests that need updating (hidden work the roadmap didn't account for):**
- `lib/test_new_tenant_script.py` (9 tests) — adapt to new CLI flags.
- `lib/test_agent_layer.py` (30 tests) — at least 3 of these touch persona content (search for "CLAUDE.md" / "morty" in test bodies); pin them against the new placeholder-free output.
- `lib/test_docs_consistency.py` (5 tests) — verify SPEC-010 still aligns.

**Acceptance criteria:**
- `./scripts/new-tenant.sh marie --type=client --owner-name="Marie Dupont" --owner-locale=fr --voice-style=vouvoiement --concierge-name=Sandra` produces a `bubble-vps-data/tenants/marie/persona/sandra/` tree with ZERO references to morty/joris/Lab/Ricky/bubble-internal.
- Grep on the rendered tree: `grep -ri "morty\|joris\|Lab\|Ricky\|bubble-internal" bubble-vps-data/tenants/marie/persona/` returns 0 lines.
- All 30+ existing tests still green.

**Risks + mitigations:**
- *Subtle copy bleed* (e.g. "I am Morty, Lab's cloud counterpart" in a memory file gets templated to "I am Sandra, Lab's cloud counterpart" — wrong). Mitigation: test #6 above is the gate. Also: agent-memory files SHOULD NOT be templated — they should be EMPTY for a fresh tenant. Only CLAUDE.md and cwd-CLAUDE.md get templated.
- *Voice style is harder than it looks*. "vouvoiement" isn't just a global s/tu/vous; it changes verb endings ("tu fais" → "vous faites"). Solution: write 2 separate template files per locale (`persona-claude-md.fr-vouvoiement.j2`, `.fr-tutoiement.j2`, `.en.j2`) and select by `{locale}-{voice_style}` key. Slightly more LOC, far less brittle.

**Honest estimate: ~4h** (was 3h). +1h for the voice/locale matrix + the 3 existing-test updates.

---

### Sprint 2 — bubble-ops-loop as a deployable dependency [HIGH confidence]

Original estimate: 4h. Refined: **~6–7h**. This is the most underestimated sprint.

**Files created/modified:**
- New: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/framework/deploy.py` — new task module, called from `deploy.py` between agent and access layers.
- New: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/framework/_install.py` — clone bubble-ops-loop or unpack tarball into `/opt/bubble-ops-loop/` (read-only, root-owned), symlink `/usr/local/bin/bootstrap-dept`, `/usr/local/bin/activate-dept`, etc.
- New: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/framework/_skills_link.py` — symlink `department-onboarding-guide` into `/home/claude/.claude/skills/` (alongside the persona-shipped skills).
- Modify: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/bootstrap-dept.sh:102` — change `GITHUB_OWNER="vdk888"` to read from env: `GITHUB_OWNER="${BUBBLE_GITHUB_OWNER:-vdk888}"`.
- Modify: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/activate-dept.sh:100` — same env-var swap.
- Modify: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/lib/cancel_eclosion.py:51` — `DEFAULT_GH_ORG = os.environ.get("BUBBLE_GITHUB_OWNER", "vdk888")`.
- Modify: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/deploy-to-morty.sh` — rename to `deploy-dept-systemd.sh`, parameterize MORTY → `${TENANT_AGENT_USER}/${TENANT_PERSONA_NAME}`. The "do not touch claude-agent-morty.service" doctrine becomes "do not touch claude-agent-${persona}.service".
- Modify: `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/deploy.py` — add `framework.apply()` call between line 62 (agent) and 68 (tailscale).
- New: `bubble-ops-loop/RELEASE.md` or `VERSION` file — versioning scheme so the VPS platform can pin a known-good ref.

**Hidden assumptions in bubble-ops-loop that break multi-tenant:**
- `deploy-to-morty.sh:8` line "DO NOT touch /etc/systemd/system/claude-agent-morty.service" — needs to become parameterized doctrine.
- `bootstrap-dept.sh:115` line `BOT_HANDLE="bubbleops${SLUG_COMPACT}_bot"` — works fine multi-tenant (it's slug-based), but: the global Telegram namespace is shared. For tenant "acme" eclosing Maya, the handle becomes `bubbleopsmaya_bot` — same as our internal Maya. Collision. **Fix: prefix with tenant**: `bubbleops${TENANT_NAME_COMPACT}_${SLUG_COMPACT}_bot`. Eats into the 32-char limit fast.
- `scripts/lib/scaffold.py:412` URL hardcodes `vdk888`. Env-var as above.
- `scripts/lib/scaffold.py:244` references "Karpathy skills repo" GitHub URL — that's fine (it's a citation of an external public repo, not our infra).
- `bootstrap-dept.sh:188` operator-set-secret invocation hardcodes `--remote-prompt=hetzner` and `--project=/etc/bubble/secrets-${SLUG}.sops.env` — both assumptions specific to our internal layout. Multi-tenant: parameterize via env or detect from the calling context.

**Data shapes / env additions:**
- New env vars consumed by bubble-ops-loop scripts: `BUBBLE_GITHUB_OWNER`, `BUBBLE_TENANT_NAME`, `BUBBLE_AGENT_USER` (default `claude`), `BUBBLE_PERSONA_NAME` (the concierge name, e.g. `sandra`).
- These propagate from `tenant.yaml` via systemd EnvironmentFile or from the operator's shell when invoked from `new-tenant.sh`.

**Helper functions:**
- `tasks/framework/_install.py::install_framework(version: str, install_dir: str = "/opt/bubble-ops-loop") -> None`
  *Clones or downloads a versioned bubble-ops-loop tarball into install_dir. Owns it root:root 0755. Symlinks key scripts into /usr/local/bin.*
- `tasks/framework/_skills_link.py::link_framework_skills(skills_dir: str = "/home/claude/.claude/skills") -> None`
  *Creates symlinks for department-onboarding-guide into skills_dir, owned by claude:claude.*

**Tests to ship (count: 10):**
1. `test_framework_install_creates_install_dir`
2. `test_framework_install_idempotent` (apply twice → no diff)
3. `test_framework_install_pins_version` (passing a SHA / tag installs that exact version)
4. `test_bootstrap_dept_respects_env_github_owner` (BUBBLE_GITHUB_OWNER=acme-corp → creates acme-corp/bubble-ops-maya, not vdk888/bubble-ops-maya)
5. `test_bootstrap_dept_telegram_handle_prefixes_tenant` (tenant=acme + slug=maya → handle includes "acme")
6. `test_bootstrap_dept_handle_length_check_includes_tenant_prefix` (regression: existing test verified ≤32 char limit; verify tenant-prefixed variant)
7. `test_deploy_dept_systemd_uses_persona_var` (the renamed deploy script)
8. `test_skills_symlinked_in_home_claude_skills`
9. `test_framework_upgrade_replaces_install_dir_atomically` (write to /opt/bubble-ops-loop.new, mv on success)
10. `test_e2e_bootstrap_eclosion_against_fresh_tenant` (heaviest — see Sprint 5)

**Existing tests to update:**
- bubble-ops-loop has its own test suite (location: `bubble-ops-loop/tests/` — TODO: count tests there). Anything that asserts hardcoded "vdk888" or "morty" will need updating. Estimated 5–10 affected tests.

**Acceptance criteria:**
- pyinfra deploy against a fresh tenant box installs `/opt/bubble-ops-loop/scripts/bootstrap-dept.sh` and `/usr/local/bin/bootstrap-dept` symlinks.
- The concierge (Sandra) running on the box can invoke `bootstrap-dept --slug=maya --display-name="Maya" --owner=marie` and it creates the dept under the tenant's GitHub org (not vdk888).
- `apt-get autoremove` would not pick up bubble-ops-loop (it's manually installed, not a dpkg) — document the upgrade flow.

**Risks + mitigations:**
- *Versioning is hard, especially with the wiki / git-guard / token-broker components*. Mitigation: pin a single SHA at install time, store it at `/opt/bubble-ops-loop/.version`, never auto-upgrade.
- *Framework changes break tenants*. Mitigation: every upgrade goes through a release branch + a smoke test on bubble-internal before promotion. This is part of "Day-2 ops" below.
- *Telegram handle collisions* (already documented above). Mitigation: include tenant prefix in handles. Cost: shorter slug max.

**Honest estimate: ~6–7h.** This is the sprint where rollback / versioning sneaks in. If we skip versioning, we'll regret it on the first framework breakage.

---

### Sprint 3 — Concierge skills + gate policies [MED confidence]

Original estimate: 3h. Refined: **~4h**.

**Files created/modified:**
- New skill: `bubble-vps-data/tenants/bubble-internal/persona/morty/skills/dept-supervisor/SKILL.md` (+ supporting `skill_lib/*.py`). Eventually moves to a shared location once stable.
- New: `bubble-vps-data/tenants/<name>/gate_policies.yaml` — per-tenant declarative policies (or embedded under `tenant.yaml::concierge.gate_policies`).
- Modify: `lib/tenant_loader.py` — parse the gate_policies block.
- New: `lib/gate_policy_loader.py` — load + validate the 5-mode enum (manual_required, auto_with_veto_window, auto_if_policy_passed, etc.).

**Data shape (gate_policies):**
```yaml
concierge:
  gate_policies:
    restart_stuck_dept:
      mode: auto_with_veto_window
      veto_window_minutes: 5
      max_per_24h: 3
    cancel_eclosion:
      mode: manual_required
    housekeeping_disk_cleanup:
      mode: auto_if_policy_passed
      max_bytes_freed: 1073741824   # 1 GB
      forbidden_paths: ["/etc", "/home/claude/.claude/agent-memory"]
    push_framework_upgrade:
      mode: manual_required
```

**Helper functions:**
- `skill_lib/dept_supervisor.py::observe_dept(slug: str) -> DeptStatus`
  *Returns systemd unit status, journalctl last-100-lines hash, heartbeat freshness, queue depth (from STATE.yaml or heartbeats.jsonl).*
- `skill_lib/dept_supervisor.py::propose_action(status: DeptStatus, policy: GatePolicy) -> ProposedAction`
  *Maps observation to one of: no_action, autonomous_action, veto_window_action, propose_for_approval.*
- `skill_lib/dept_supervisor.py::execute_with_veto(action: ProposedAction, window_minutes: int) -> ExecutionResult`
  *Sends Telegram message, waits N minutes, executes if not vetoed, logs to audit trail.*

**Tests to ship (count: 8):**
1. `test_gate_policy_loads_5_modes`
2. `test_gate_policy_rejects_invalid_mode` (e.g. "shadow_autonomy" — doctrine fix from PR #4)
3. `test_observe_dept_returns_systemd_status`
4. `test_propose_action_manual_required_never_acts`
5. `test_propose_action_auto_with_veto_window_sends_telegram_first`
6. `test_execute_with_veto_respects_window` (mock time, verify wait)
7. `test_execute_with_veto_short_circuits_on_veto_keyword` ({{OPERATOR}} types "stop" → action aborted)
8. `test_housekeeping_respects_forbidden_paths`

**Acceptance criteria:**
- On bubble-internal, Sandra can run `Skill dept-supervisor observe maya` and it returns a real status dict.
- A simulated "maya is stuck for 2h" → Sandra sends a Telegram message proposing restart, waits 5min, restarts if no veto.
- An attempt to delete `/etc` via housekeeping_disk_cleanup is rejected by the forbidden_paths check.

**Risks + mitigations:**
- *Gate policies are easy to get wrong* (e.g. auto_with_veto_window with a 0-minute window is effectively "auto"). Mitigation: validation enforces `veto_window_minutes >= 1` for that mode.
- *The veto channel is Telegram, which has its own outages*. Mitigation: if Telegram unreachable, downgrade auto_with_veto_window → manual_required for that incident.

**Honest estimate: ~4h.** Skills are conceptually simple but the gate-policy state machine + audit log adds ~1h.

---

### Sprint 4 — Bubble remote access [HIGH confidence]

Original estimate: 2h. Refined: **~3h**.

**Files created/modified:**
- New: `pyinfra/tasks/access/bubble_admin.py` — manages `/root/.ssh/authorized_keys` and `/home/claude/.ssh/authorized_keys` based on `tenant.yaml::bubble_admin_keys`.
- New template: `pyinfra/templates/bubble-admin-keys.j2` — header comment + key list.
- New: `pyinfra/tasks/access/audit_log.py` — install rsyslog rule that pipes `bubble_admin`-authenticated SSH logins to a dedicated file `/var/log/bubble-admin-access.log` (tenant-readable).
- Modify: `deploy.py` — call `bubble_admin.apply()` after `tailscale.apply()`.

**Data shape:** already covered in Sprint 0.

**Helper functions:**
- `tasks/access/bubble_admin.py::sync_admin_keys(cfg: TenantConfig) -> None`
  *Renders authorized_keys with bubble_admin_keys entries marked with `# bubble-admin:<name> expires:<date>` comments. Filters expired keys (warning to operator on Telegram).*

**Tests to ship (count: 6):**
1. `test_bubble_admin_keys_written_to_authorized_keys`
2. `test_expired_keys_filtered_out` (key with expires_at < today → not written)
3. `test_expired_key_triggers_telegram_warning`
4. `test_bubble_admin_keys_remove_when_block_removed_from_yaml` (key in v1, removed in v2 → next deploy removes it)
5. `test_no_bubble_admin_block_means_no_extra_keys` (opt-in via presence of block)
6. `test_audit_log_captures_bubble_admin_ssh_login` (integration; might be moved to E2E)

**Acceptance criteria:**
- Rick can SSH into a tenant box as the `claude` user (or root if granted) using his pubkey listed in bubble_admin_keys.
- Removing rick's entry from tenant.yaml → next deploy → Rick can no longer SSH.
- An expired key triggers a Telegram alert to both the operator ({{OPERATOR}}) AND the tenant.

**Risks + mitigations:**
- *Tenant secrecy*: the tenant may be uncomfortable knowing Bubble has root. Mitigation: the audit log is REQUIRED (tenant.yaml schema validation rejects bubble_admin_keys without audit_log enabled), and the tenant can `tail -f /var/log/bubble-admin-access.log`.
- *Stale keys*: 6-month expiry default + Telegram warning on next deploy. Hard fail if expired by >30 days.
- *Key sharing within Bubble*: each engineer gets their own entry with their own pubkey. No shared "bubble" key.

**Honest estimate: ~3h.** +1h for the audit log + Telegram warning logic that wasn't in the original.

---

### Sprint 5 — End-to-end test "fresh tenant Marie" [HIGH confidence]

Original estimate: 2h. Refined: **~4h**.

This is the integration sentinel. Honest re-estimate matters here because the test plan touches every other sprint.

**Files created/modified:**
- New: `tests/integration/test_fresh_tenant_marie.sh` — orchestrates the full walk.
- New: `lib/test_fresh_tenant_e2e.py` — unit-level companion that mocks SSH/network to validate the script's flow.

**Test plan (each step is a separately-assertable checkpoint):**
1. `new-tenant.sh marie --type=client --owner-name="Marie Dupont" --owner-locale=fr --voice-style=vouvoiement --concierge-name=Sandra --owner-telegram=12345` → check tenant dir created, secrets.sops.env encrypted, persona/sandra/CLAUDE.md has no morty/joris/Lab strings.
2. `provision-tenant.sh marie` → check Hetzner box created (or mocked in CI).
3. `operator-set-secret.sh --tenant=marie` for each required key.
4. `./deploy.sh --tenant=marie` → check all 8 layers green (hardening/secrets/agent/tailscale/watchdog/audit/phone-home/wiki-sync) + framework layer (new).
5. SSH into the box, verify `/etc/systemd/system/claude-agent-sandra.service` is active, `/opt/bubble-ops-loop/` exists, `~/.claude/skills/department-onboarding-guide/` symlinked.
6. Send `/start` to Sandra's Telegram bot → receive a vouvoiement French greeting (NOT Morty's tutoiement English).
7. Ask Sandra "éclôs-moi un dept Maya" → she invokes `bootstrap-dept --slug=maya --owner=marie` → a GitHub repo gets created (or mocked) under the configured org.
8. SSH into the box as `rick@bubbleinvest` (via bubble_admin_keys) → access granted, login recorded in `/var/log/bubble-admin-access.log`.
9. `offboard-tenant.sh marie --mode=destroy` → box destroyed, tenant dir archived.

**Tests to ship (count: 6):**
1. `test_e2e_dryrun_walks_all_9_steps` (mocked SSH, fake Telegram).
2. `test_e2e_step1_persona_no_morty_strings`
3. `test_e2e_step5_sandra_service_active`
4. `test_e2e_step6_telegram_greeting_voice_style` (mock Telegram response)
5. `test_e2e_step8_bubble_admin_audit_logged`
6. `test_e2e_step9_offboard_destroys_cleanly`

**Acceptance criteria:**
- The full integration test (with mocks) runs in <60s locally.
- The real integration test (against a fresh Hetzner box) runs in <30min including provisioning, deploys without operator intervention beyond the initial CLI invocation.
- Total deploy time (after secrets pasted) <8min for the "happy path."

**Risks + mitigations:**
- *Hetzner provisioning flake*. Mitigation: provision once at start of day, run E2E test 5x in a row, then teardown. Use `provider_server_id` re-use if possible.
- *Telegram bot creation can't be automated* (BotFather is conversational). Mitigation: the test pre-creates the bot once, stores the token in 1Password, the test injects it.
- *Cost*: each E2E run = ~€0.10 in Hetzner CX22 hourly billing. Run it nightly, not per-commit.

**Honest estimate: ~4h.** +2h over original because: real Hetzner integration (vs mocks), Telegram mocking infrastructure, the "delete tenant" verification.

---

## 3. Open questions resolved

### Q1 — GitHub org for tenants' repos
**Default answer: create a GitHub Organization `bubbleinvest-tenants` NOW; use it for ALL paying clients; keep `vdk888` for internal/test tenants only.**

Reasoning: (a) Free Org plan covers unlimited public + 5 private repos — fine for the first 5 paying clients. (b) `vdk888` is {{OPERATOR}}'s personal handle; mixing client deliverables under a personal account is unprofessional and creates an exit problem if {{OPERATOR}} ever leaves Bubble Invest. (c) An Org gives us audit logs + per-team permissions for free. (d) Tenant config can override: `tenant.yaml::framework.github_org: "their-own-org"` for clients that prefer their own GitHub.

Cost: 30min to create the Org + invite {{OPERATOR}} + Rick. Affects `bootstrap-dept.sh:102` and `activate-dept.sh:100` (both already in Sprint 2 scope). Confidence: HIGH.

### Q2 — DNS for client-facing console
**Default answer: Tailscale-only for v1; revisit when first client asks for public URL.**

Reasoning: (a) Public DNS = Let's Encrypt cert renewal = another moving part = more support burden. (b) The Bubble Cabinet north star says "zero outbound except api.anthropic.com + api.telegram.org" — public-DNS-with-TLS isn't strictly forbidden but it adds attack surface. (c) The console is for the OWNER; the owner already has Tailscale (we'll install it on their phone). (d) When a client insists, the cost is ~2h: provision `marie.cabinets.bubbleinvest.com` → certbot → reverse-proxy on the VPS. Not a hard "no", just a "not yet."

Trade-off {{OPERATOR}} should validate: are there clients (lawyers, doctors) for whom Tailscale-on-their-phone is a non-starter? If yes, raise Sprint 4 scope to include public DNS. Confidence: MED.

### Q3 — Pricing model
**Default answer: Bundled "Cabinet Starter" at €149/mo (VPS + concierge + framework + 1 dept included); each additional dept = €30/mo. À la carte unlocks at €299/mo "Pro" plan.**

Reasoning: (a) Bundled is simpler to sell ("one bill") and to compute COGS (VPS €5 + Anthropic Opus ~€60/mo concierge + ~€40/mo per dept). (b) €149 leaves ~€85/mo margin per starter — viable. (c) The schema needs `billing.plan` (enum) + `billing.dept_count` (int) for billing reconciliation, NOT enforcement. We bill manually for the first 10 clients; automate later. (d) "Pro" plan exists to capture clients who'll want 3+ depts and value the unbundling.

Risk: the per-dept marginal Anthropic cost depends heavily on dept activity. We need a 30-day live run to know the real number. Until then, treat €30/mo as a placeholder. Confidence: LOW — {{OPERATOR}} and {{OPERATOR_2}} (GM) should validate.

### Q4 — Concierge model choice
**Default answer: Sonnet 4.5 by default ("Cabinet Starter"); Opus 4.7 only on "Pro" plan or by explicit upgrade.**

Reasoning: (a) The concierge is a low-cognitive-load role — observation + housekeeping + escalation. Sonnet 4.5 is plenty (Maya's onboarding ran on Sonnet for the agent-side reasoning in tests). (b) Opus 4.7 costs ~3× more per token. For a concierge running 24/7 with frequent journalctl/grep/status calls, this is non-trivial. (c) Opus 4.7's edge (deep reasoning) is wasted on "check if maya is stuck" — that's pattern matching. Save Opus for the depts that actually need it. (d) `tenant.yaml::agent.llm.model` is already schema-flagged ("opus" or "sonnet"). No code change needed beyond defaulting Sonnet in the new-tenant.sh template.

Cost implication: ~€20/mo savings per tenant on the concierge alone. At 10 tenants that's €200/mo direct margin. Confidence: HIGH (technical), MED (market — premium feel may matter for "Pro" perception).

### Q5 — Off-site backup target
**Default answer: Backblaze B2 by default; offer Hetzner Storage Box upgrade for tenants who want EU-only / single-vendor.**

Reasoning: (a) Vendor isolation matters more than €2/mo savings — if Hetzner has an outage that takes down the VPS, we want the backup on a DIFFERENT provider. (b) B2 is ~€1/mo per 200GB — well below Hetzner Storage Box (€3.81/mo flat). (c) B2's S3-compatible API is universal — easier to migrate off than Hetzner's proprietary SMB. (d) GDPR: B2 has EU regions (Amsterdam), satisfies most clients. For a strict-EU-jurisdiction client (gov, healthcare), offer Hetzner Storage Box as upgrade.

Risk: Restic + B2 requires a `RESTIC_REPOSITORY` URL and `B2_ACCOUNT_ID/B2_ACCOUNT_KEY` in the secrets. That's 2 new required_keys in tenant.yaml. ~30min schema work. Confidence: HIGH.

---

## 4. Day-2 operations gap analysis

### 4.1 Framework upgrades to live tenants
- **Status:** MISSING.
- **Where it lives today:** Nowhere. The `deploy-to-morty.sh` script pushes the framework to a single hardcoded box; there's no fleet-aware upgrade path.
- **Proposal:** New script `scripts/upgrade-framework-fleet.sh` that:
  1. Pins the new version (git SHA or tag).
  2. For each tenant in `TENANTS_ALL`: run pyinfra in upgrade-only mode (skip secrets+agent+hardening), redeploy ONLY the framework layer.
  3. Smoke-test post-upgrade: SSH in, run a no-op `bootstrap-dept --dry-run`, verify exit 0.
  4. On failure: rollback to previous version (atomic symlink swap of `/opt/bubble-ops-loop/`).
  5. Telegram report to operator + tenant.
- **Adds to: NEW Sprint 6 (post-MVP).** ~3h.

### 4.2 Tenant VPS in trouble — concierge alerts us
- **Status:** PARTIALLY COVERED. The `security_audit` cron (SPEC-014) posts daily to {{OPERATOR}}'s Telegram. The `telegram_watchdog` (SPEC-013) posts on stale heartbeat. The `phone_home` daemon (SPEC-015) reports operational metadata to a central dashboard.
- **Gap:** None of these alert RICK/JORIS to a CLIENT's VPS in trouble — they alert THE CLIENT'S concierge → the client's owner. Cross-tenant escalation is missing.
- **Proposal:** Add a `bubble_invest_oncall.telegram_chat_id` field in tenant.yaml. The security_audit and telegram_watchdog templates check: on CRITICAL severity (e.g. disk >95%, fail2ban shows persistent attacker), also post to `bubble_invest_oncall.telegram_chat_id`. Default off; tenant opts in (legal/comms reason: "we don't watch you, you watch you" — opt-in flips that).
- **Hidden assumption:** the templates currently hardcode `JORIS_TG_USER_ID="{{ joris_telegram_user_id }}"` (pyinfra/templates/security-audit.sh.j2:21). Rename in Sprint 0 to `OPERATOR_TG_USER_ID` + add `ONCALL_TG_USER_ID` as 2nd channel.
- **Adds to: NEW Sprint 6.** ~2h.

### 4.3 Tenant cancellation / self-service decommission
- **Status:** PARTIALLY COVERED. `offboard-tenant.sh` (Scenario A handoff / Scenario B destroy) exists at `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/scripts/offboard-tenant.sh`. It's operator-driven.
- **Gap:** No self-service path. A client who wants to cancel must email {{OPERATOR}}/Rick. Acceptable for v1 (low churn assumption). Not acceptable past 20 clients.
- **Proposal:** v1 keep manual. v2 (>20 clients): Sandra herself can initiate offboarding via a `concierge_can_offboard` gate policy. The flow becomes: client tells Sandra "I want to cancel" → Sandra confirms with 7-day waiting period → Sandra runs offboard-tenant.sh remotely. This is doable but adds risk surface; NOT for MVP.
- **Adds to: SPRINT 6 or LATER.** Not needed for first 5 paying clients.

### 4.4 SLA
- **Status:** ZERO PROMISES TODAY.
- **Proposal for "Cabinet Starter":** Best-effort, 24h response on business days, 95% uptime monthly (excluding planned maintenance). No financial credits.
- **Proposal for "Cabinet Pro":** 4h response (business hours), 99% uptime monthly, financial credits on miss (capped at 1 month subscription).
- **What this implies operationally:** A `on-call` channel (Telegram group) staffed by {{OPERATOR}}+Rick. Phone-home + security audit dashboards must be visible to Rick at all times. SLA doc + status page (e.g. `status.bubbleinvest.com`) for transparency.
- **Adds to: NEW Sprint 7 — SLA + operational readiness.** ~4h doc + ~2h status page.

### 4.5 Support model
- **Status:** UNDEFINED.
- **Proposal:**
  - **Tier 1 (concierge):** the concierge handles all routine ops in-conversation with the owner. Restart depts, clean disk, explain what's happening.
  - **Tier 2 (Sandra escalates to Bubble Invest):** for novel issues or root-required interventions, Sandra posts to a Bubble Invest oncall Telegram group. The tenant sees what was shared (no hidden channels).
  - **Tier 3 ({{OPERATOR}}/Rick login):** SSH via bubble_admin_keys. Always audited (Sprint 4 covers this).
- **Adds to: SPRINT 6.** ~1h doc, the mechanism is built in Sprints 3 (concierge skills) + 4 (admin keys).

---

## 5. Re-sequencing recommendation

**Original sequence:** Sprint 1+2+5 first (MVP), Sprint 3+4 later.

**My recommendation: Sprint 0 + Sprint 1 + Sprint 5 first. Defer 2 + 3 + 4.**

Rationale:
- **Sprint 0 (schema)** is foundational for 1+3+4 — doing it first makes downstream work 30% faster (no churn). ~3h, hard to justify skipping.
- **Sprint 1 (persona templating)** is the user-visible payoff. After Sprint 1, "we can scaffold a fresh tenant directory with a Sandra-shaped persona" — that's already worth demo'ing.
- **Sprint 5 (E2E test)** is what gives confidence the chain holds. Sprint 5 against a NO-Sprint-2-yet world means: Sandra deploys, talks to Marie on Telegram, but can't yet éclore Maya — that's STILL a sellable starting point if we frame it right ("the concierge today, depts shipping in 2 weeks").
- **Sprint 2 (bubble-ops-loop deployable)** is the biggest sprint. Doing it AFTER Sprint 5 shows there's a real concierge first; then we can size the framework deployable correctly.
- **Sprint 3 (concierge skills)** assumes Sprint 2 (depts to supervise). Without depts, Sandra has nothing to observe. Defer until Sprint 2 lands.
- **Sprint 4 (bubble_admin_keys)** is independently useful (we already need it to debug bubble-internal). Pull it into Sprint 0 actually — it's 90% schema work + 10% pyinfra. **Revised: do Sprint 4 as part of Sprint 0.**

**Final recommended sequence:**
1. **Sprint 0+4 combined** (schema + bubble_admin_keys) — ~5h
2. **Sprint 1** (persona templating) — ~4h
3. **Sprint 5 lite** (E2E against the "concierge only" world) — ~2h
4. **DEMO to first prospect** — concierge works, depts coming
5. **Sprint 2** (bubble-ops-loop deployable) — ~6–7h
6. **Sprint 3** (concierge skills + gate policies) — ~4h
7. **Sprint 5 full** (E2E with depts) — ~2h
8. **Sprint 6** (Day-2 ops: upgrades, oncall, support tiers) — ~6h
9. **Sprint 7** (SLA + status page) — ~6h

**Total: ~36h** vs the original 14h, but the original was missing Day-2 ops entirely. For just "MVP demoable" the cost is ~11h (sprints 0+4, 1, 5-lite).

---

## 6. Shared-with-Bubble-Cabinet map

Comparing the 5 T-a-a-S sprints against the 3 Bubble Cabinet sprints (`NORTH-STAR-BUBBLE-CABINET.md`):

| T-a-a-S Sprint | Shared with Bubble Cabinet? | % shared | Notes |
|---|---|---|---|
| 0 — Schema additions | 80% shared | Persona+owner blocks are identical; bubble_admin_keys is cloud-only; `billing` is cloud-only (Cabinet is one-shot sale) | Do Sprint 0 ONCE, both products benefit |
| 1 — Persona templating | 100% shared | Same `persona-claude-md.j2` template; same voice/locale matrix; Cabinet feeds it from `.env`, T-a-a-S feeds from tenant.yaml — pure render function | This is THE sprint where the two products converge |
| 2 — bubble-ops-loop deployable | 80% shared | Same `bootstrap-dept`/`activate-dept` parameterization; Cabinet adds the `local-bare` git-provider mode (Cabinet Sprint 2, ~4h); T-a-a-S keeps GitHub mode | The env-var swap (BUBBLE_GITHUB_OWNER) is shared; the `local-bare` adapter is Cabinet-only |
| 3 — Concierge skills + gate policies | 100% shared | The dept-supervisor skill works identically — Cabinet's Sandra needs the same observe/restart/cleanup powers | Build once, ship both |
| 4 — Bubble remote access (bubble_admin_keys) | 30% shared | Cabinet runs ON THE CLIENT'S OWN INFRA — `bubble_admin_keys` is more delicate (DSI may refuse). The schema is shared, the default may differ (opt-in vs opt-out) | Cabinet might default `bubble_admin_keys: []` (no remote access by default); T-a-a-S defaults non-empty (we own the box) |
| 5 — E2E test | 50% shared | The script structure is identical; the verbs differ (Hetzner provision vs Docker compose up). Half the assertions transfer | Maintain TWO E2E tests; share the assertion helpers |
| (Day-2: framework upgrades) | 90% shared | Both products need it; Cabinet uses `docker compose pull` flow, T-a-a-S uses pyinfra redeploy | Share the smoke-test helper |
| (Day-2: SLA) | 0% shared | Cabinet is on-premise → no SLA from us; T-a-a-S we host → we have SLA | Independent |

**Recommendation: build Sprints 0+1+3 product-agnostically.** Treat T-a-a-S and Bubble Cabinet as two delivery surfaces over the same core. This means the "core" (persona templating, dept supervisor, framework parameterization) lives in `bubble-ops-loop` OR a new shared dir `bubble-vps-platform/lib/shared/`; the surfaces (pyinfra for T-a-a-S, Dockerfile for Cabinet) consume the core.

**If you only build Bubble Cabinet sprints 1+3** (Dockerfile + READMEs), you get 70% of T-a-a-S Sprint 1+2 for free.

---

## 7. Deal-breakers

Five things that, if they don't work, kill T-a-a-S:

### DB1 — The concierge can observe depts WITHOUT reading their Telegram conversations
- **Why critical:** Doctrine constraint from the roadmap ("Control plane ≠ data plane"). If Sandra can read Maya's Telegram, Marie's privacy expectation is broken and we can't sell "your owner sees what Maya talks about, not us, not Sandra."
- **Current plan addresses it:** Partially. Each dept has its own bot token, isolated by systemd EnvironmentFile (per `tenant.yaml::agent.channels.telegram.bot_token_secret_ref`). Sandra's systemd unit reads ONLY her own env file.
- **What could go wrong:** Skills like `dept-supervisor` might invoke `journalctl -u claude-agent-maya.service` which DOES capture stdout/stderr from Maya — that COULD include Telegram message bodies if Maya logs them. Mitigation: enforce in Maya's CLAUDE.md "never log message bodies to stdout"; add a regex audit in `morty-agentic-audit.sh` that fails if a Telegram-message-shaped string appears in any journalctl output.
- **Confidence: MED.** Need to verify Maya/Ben actually don't log message bodies. TODO: verify with {{OPERATOR}}.

### DB2 — A tenant boots a malicious dept (intentionally or accidentally) that exfiltrates secrets
- **Why critical:** A dept eclosure includes "what skills can this dept call?" If a dept gets `Bash` access (most do), it can `cat /run/claude-agent/env` (oh wait — that's root-owned, 0400, only the agent's UID reads it) — but it CAN read its OWN env file. If a dept's env includes `GITHUB_TOKEN` (cross-tenant, scoped to bubble-shared-wiki), exfil possible.
- **Current plan addresses it:** Per-dept secrets file (`/etc/bubble/secrets-<slug>.sops.env`). Sandra (concierge) DOES NOT have access to that file by default.
- **What could go wrong:** Sloppy template that puts `GITHUB_TOKEN` in EVERY dept's env regardless of need. Audit: each dept's tenant.yaml block must explicitly list required_keys; SHARED keys (PHONEHOME_TOKEN) are concierge-only.
- **Confidence: HIGH** that the pattern is correct, **MED** that current code enforces it strictly.

### DB3 — pyinfra deploy is not idempotent across tenants (running TENANT=marie ./deploy.sh causes state changes on bubble-internal)
- **Why critical:** A multi-tenant deploy MUST never touch a different tenant's state. The current `inventory.py:107` uses `cfg.host.ip` as the SSH target — this is per-tenant correct. The dashboard hardcode (`_DASHBOARD_HOST_TENANT = "bubble-internal"` at `dashboard.py:59`) is a one-way gate (other tenants no-op).
- **Current plan addresses it:** Sprint 0 schema work replaces the hardcode with a per-tenant flag. Good.
- **What could go wrong:** Some pyinfra ops (e.g. systemctl daemon-reload) are box-local — fine. But ANY shared cron/dashboard would be cross-tenant. Audit needed: grep for any cross-tenant write paths in the codebase.
- **Confidence: HIGH** based on the inventory architecture (SSH to tenant.host.ip only).

### DB4 — Sandra (concierge) crashes and there's no recovery without Rick logging in
- **Why critical:** If Sandra crashes and her systemd `Restart=on-failure` loop also fails (e.g. her env file is corrupt, age decryption fails, claude binary missing), Marie has zero recourse short of calling Bubble.
- **Current plan addresses it:** Partially. The `telegram_watchdog` (SPEC-013) detects stale heartbeats. The `security_audit` (SPEC-014) reports daily. But neither REPAIRS Sandra.
- **What could go wrong:** A bad framework upgrade pushes a broken systemd unit; Sandra fails to start; Marie sees nothing for hours. Mitigation: bubble_admin_keys (Sprint 4) lets Rick SSH in and recover. Telegram alert (Day-2 §4.2) routes critical to Bubble oncall.
- **Confidence: MED.** This is exactly why Sprint 4 + Day-2 oncall channel are mission-critical and shouldn't be deferred to "v2".

### DB5 — Telegram is the only channel and Telegram has an outage
- **Why critical:** Telegram has had multi-hour outages (e.g. 2024-Q2). If Telegram is down: Marie can't talk to Sandra, Sandra can't escalate, the watchdog can't alert.
- **Current plan addresses it:** Not at all.
- **What could go wrong:** A Telegram outage during business hours, on day 3 of a paying client. They can't reach their concierge. Bad look.
- **Mitigation options (not in current plan):** (1) Secondary channel — email via Mailgun for critical alerts. (2) Web console (Cabinet north star §"console web exposée sur localhost") — but that's Cabinet-side, not T-a-a-S yet. (3) Accept the risk + document in the SLA ("dependent on Telegram availability"). Recommended: (3) for v1, (1) as Sprint 7 add.
- **Confidence: HIGH** that this is a real risk. **LOW** confidence on whether clients care enough to delay shipping.

---

## Appendix — Files I read to produce this refinement

For traceability:

- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/docs/ROADMAP-TENANT-AS-A-SERVICE.md` (147 lines)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/docs/NORTH-STAR-BUBBLE-CABINET.md` (223 lines)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/docs/ARCHITECTURE.md` (skim)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/docs/SECURITY.md` (skim, threat model)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/docs/OFFBOARDING.md` (header)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/deploy.py` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/inventory.py` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/lib/tenant_loader.py` (full, ~778 lines)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/agent/deploy.py` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/agent/_persona.py` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/agent/_systemd_unit.py` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/access/tailscale.py` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/tasks/monitoring/dashboard.py` (skim, hardcode confirmation)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/scripts/new-tenant.sh` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/persona-claude-md.j2` (stub, 1 line)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/tenant.yaml.j2` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/pyinfra/templates/claude-agent.service.j2` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-data/tenants/bubble-internal/tenant.yaml` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-data/tenants/bubble-internal/persona/morty/CLAUDE.md` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-vps-data/tenants/bubble-internal/persona/morty/cwd-CLAUDE.md` (full)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/bootstrap-dept.sh` (partial: lines 90-200)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/activate-dept.sh` (partial)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/scripts/deploy-to-morty.sh` (header)
- `/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/skills/department-onboarding-guide/SKILL.md` (header)
- Grep audits for "morty", "joris", "bubble-internal", "vdk888" hardcoding patterns across the pyinfra and bubble-ops-loop trees.

I did NOT read (per the brief mentions): `/tmp/notion_final.txt` (file not opened — could not verify the line citations in the SKILL.md doc; if {{OPERATOR}} wants those validated, ping me). RUNBOOK.md, INSTALL.md, ONBOARDING.md — skimmed via `ls` only, content not used.

**TODOs flagged for {{OPERATOR}} validation:**
- Q3 pricing — placeholder numbers (€149/€299/€30) need market validation.
- Q5 backup — Backblaze B2 vs Hetzner Storage Box, validate the GDPR/EU stance.
- DB1 — confirm that depts (Maya, Ben) don't log Telegram message bodies to stdout.
- Sprint 1 — confirm voice-style matrix (3 templates × 2 locales vs single param) is acceptable scope.
- Sprint 6/7 (Day-2) — confirm we should bundle these into the T-a-a-S roadmap or split into a separate "Operate-a-Tenant" workstream.
