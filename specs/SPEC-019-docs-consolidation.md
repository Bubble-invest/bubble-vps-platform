# SPEC-019 — Documentation consolidation pass (Step 7d)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 7a/7b/7c done (so docs can describe the full happy path)
**Implements:** Step 7d of the Bubble VPS Platform build plan — closes litmus criterion #6 (hand the platform to another engineer who can run a deploy)

---

## Purpose

Right now we have 18 specs (SPEC-001 through SPEC-018) and a few scattered runbooks/READMEs. Each spec is good on its own but the platform's overall narrative is hard to assemble for a newcomer. An outside engineer would have to read all 18 to figure out "how do I deploy this?".

This spec produces the missing top-level narrative:

1. **`README.md`** at platform repo root — rewritten as a real product README (what is this, who uses it, link to setup)
2. **`docs/INSTALL.md`** — already exists, expand to a full "0 → working tenant" walkthrough
3. **`docs/ONBOARDING.md`** — new — operator-facing playbook for adding a paying client
4. **`docs/OFFBOARDING.md`** — new — companion to ONBOARDING (per Step 7c)
5. **`docs/RUNBOOK.md`** — already exists from Step 2, expand with newer ops content (debugging Telegram, recovering a dead tenant, etc.)
6. **`docs/ARCHITECTURE.md`** — new — high-level design doc that points at specs for details
7. **`docs/SECURITY.md`** — new — threat model, secrets management, key rotation procedures

NOT a goal: rewriting any of the SPEC-001…018 docs. Those stay as-is, the consolidation just adds the connective tissue.

---

## Deliverables

### 1. `README.md` (platform repo root)

~80 lines. Sections:
- One-paragraph pitch ("Bubble VPS Platform — sellable infrastructure-as-code for deploying always-on Claude Code agents on hardened EU VPS, with full data sovereignty and managed access via Tailscale")
- Quick demo (`./scripts/deploy.sh --tenant=bubble-internal`)
- Architecture summary (link to `docs/ARCHITECTURE.md`)
- For operators: link to `docs/INSTALL.md`
- For developers: link to `docs/CONTRIBUTING.md` (defer this; not Phase 1)
- Status badge (X/Y tests passing)
- License (MIT or proprietary — TBD with {{OPERATOR}})

### 2. `docs/INSTALL.md` (expand existing)

Currently ~50 lines covering operator setup. Expand to ~200 lines covering:
- Prerequisites (macOS Mac, Hetzner account + API token, Tailscale account + tagOwner)
- One-time setup (operator-bootstrap-age.sh, Tailscale install, Hetzner token in Keychain)
- Per-tenant happy path:
  ```
  ./scripts/new-tenant.sh acme-corp --type=client
  ./scripts/provision-tenant.sh acme-corp
  ./scripts/operator-set-secret.sh --tenant=acme-corp --key=TELEGRAM_BOT_TOKEN
  ./scripts/operator-set-secret.sh --tenant=acme-corp --key=CLAUDE_CODE_OAUTH_TOKEN
  ./scripts/operator-set-secret.sh --tenant=acme-corp --key=TAILSCALE_AUTHKEY
  ./scripts/operator-set-secret.sh --tenant=acme-corp --key=PHONEHOME_TOKEN
  ./scripts/deploy.sh --tenant=acme-corp        # halts at Phase D first-half gate
  # operator adds box pubkey to .sops.yaml + runs sops updatekeys
  ./scripts/deploy.sh --tenant=acme-corp        # second deploy lands cleanly
  ```
- Troubleshooting section (consolidated from existing INSTALL.md + new content)

### 3. `docs/ONBOARDING.md` (new)

~100 lines. Operator-facing playbook for "deal closed → tenant operational". The actual checklist {{OPERATOR}}/Lab follow when a real client signs up:

```
# Tenant onboarding playbook

## Pre-deploy (10 min, operator)
1. Verify client has signed DPA + SLA
2. Verify client has provided their:
   - Telegram username (for the bot's allowFrom)
   - GitHub username (if they want any custom skills via PR)
   - Anthropic plan tier (Pro / Max / Team / Enterprise — affects which models work)
3. Open `bubble-vps-data/tenants/<name>/README.md` and fill in:
   - Contract details (signed date, plan, SLA tier)
   - Primary contact info
   - Billing info

## Provisioning (15 min, mostly automated)
1. ./scripts/new-tenant.sh <name> --type=client --display-name="<Name>"
2. Edit tenant.yaml — fill the placeholders the script left:
   - contact.primary_email
   - contact.primary_telegram_user_id (the client's, NOT yours)
   - agent.channels.telegram.allowed_user_ids (add the client's Telegram ID)
3. ./scripts/provision-tenant.sh <name>      # Hetzner box up
4. operator-set-secret.sh for each of the 4 required keys
5. ./scripts/deploy.sh --tenant=<name>        # halts at Phase D
6. Add box pubkey to .sops.yaml + sops updatekeys (one-time bootstrap)
7. ./scripts/deploy.sh --tenant=<name>        # second deploy lands

## Post-deploy verification (5 min, manual)
1. Smoke test: send "hello" to the client's Telegram bot, expect a reply
2. Open dashboard: http://:3848/, see tenant row
3. Check security audit ran on first deploy (or wait for tomorrow 09:00 UTC)
4. Send client their welcome email with:
   - Their bot's @username
   - The dashboard URL (if they're on tailnet) — usually NOT shared, internal only
   - Support contact info
   - SLA terms

Total: ~30 minutes from "deal closed" to "tenant operational".
```

### 4. `docs/OFFBOARDING.md` (new)

Companion to ONBOARDING. Walks through the two scenarios from SPEC-018 (handoff vs destroy) with concrete command sequences and the client communication template.

### 5. `docs/RUNBOOK.md` (expand existing)

Currently has the hardening drift test. Expand with:
- "Telegram plugin not responding" → check bot.pid, watchdog logs, manual restart
- "Dashboard not loading" → check Tailscale, check service, check tailnet IP binding
- "Deploy fails at sops verify" → check secrets file has all required keys, check ages key is correct
- "Auto-restart loop" → check journal for the failure pattern, common causes
- "Secret needs rotation" → operator-set-secret.sh + redeploy + smoke test

### 6. `docs/ARCHITECTURE.md` (new)

~100 lines. The "executive summary" of the platform. Points at SPEC-* docs for details. Sections:
- Two-repo split (platform + data)
- Three layers (hardening / secrets / agent / access / monitoring)
- Trust model (operator + tenant box, two SOPS recipients)
- Tailscale for ops access, public IP for fallback
- Phone-home + dashboard for visibility
- What we deliberately don't build (no multi-region, no SaaS UI, etc — links to end-state-vision.md)

### 7. `docs/SECURITY.md` (new)

~120 lines. The threat model + procedures. Sections:
- Threat model (operator-Mac compromise, tenant-box compromise, GitHub repo compromise, etc.) — pulls from SPEC-006's threat table
- Hardening profile summary (links to SPEC-005)
- Secrets management (SOPS+age, never-print-decrypted-to-stdout rule, file:auth-key form)
- Key rotation procedures (operator master, per-tenant box, per-secret)
- Incident response (what to do if a tenant is suspected compromised — disconnect Tailscale, rotate secrets, audit transcripts)
- Sudoers grants per cron (security audit, watchdog) — narrow scopes documented

---

## Test plan

### Static tests in `lib/test_docs_consistency.py` (new file)

1. `test_install_md_lists_all_required_keys` — parse INSTALL.md, find the secret-setup section, assert it lists all 4 of TELEGRAM_BOT_TOKEN / CLAUDE_CODE_OAUTH_TOKEN / TAILSCALE_AUTHKEY / PHONEHOME_TOKEN. Catches drift if we add a 5th required key but forget to update INSTALL.md.

2. `test_onboarding_md_command_sequence_matches_install` — both INSTALL and ONBOARDING list the deploy command sequence; assert they're consistent.

3. `test_security_md_lists_all_sops_recipients` — assert SECURITY.md mentions both operator master + per-tenant box keys.

4. `test_architecture_md_links_to_specs` — assert ARCHITECTURE.md has at least one `(SPEC-XXX)` link reference per major section.

5. `test_readme_lists_test_count` — assert README badges include actual current test count (auto-updated or hardcoded — operator updates manually, tested for presence).

These tests are LIGHT (just text-presence checks) — they catch drift, don't enforce content quality.

---

## Acceptance criteria

Step 7d done when:
1. ✅ All 7 docs exist with the structure above
2. ✅ INSTALL.md walks an outside engineer from 0 → first deploy without referencing internal Slack/Notion
3. ✅ ONBOARDING + OFFBOARDING checklists are concrete (numbered commands, not abstract)
4. ✅ ARCHITECTURE.md doesn't restate spec content — just orients + links
5. ✅ SECURITY.md captures the hard-won lessons (the "never print to stdout" rule, the operator-Mac as root-of-trust, etc.)
6. ✅ 5 new static doc-consistency tests pass
7. ✅ All previous tests still pass (182 → 187)

---

## Out of scope

- CONTRIBUTING.md (defer — no external contributors yet)
- A separate API reference doc for the pyinfra task modules (the specs serve this role)
- Diagrams (text-only for now; if {{OPERATOR}} wants visuals later, separate)
- Marketing/sales pages (different audience, different doc tree)
- Translation (we work in English internally)
