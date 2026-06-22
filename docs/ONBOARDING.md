# ONBOARDING — tenant onboarding playbook

Operator-facing checklist for the path from "deal closed" to "tenant operational". Companion to [INSTALL.md](INSTALL.md) (which is the bare-mechanics walkthrough); this doc adds the contractual / human-loop steps around it.

Total budget: ~30 minutes from "deal closed" to "client gets welcome email".

For the offboarding companion see [OFFBOARDING.md](OFFBOARDING.md).

---

## Pre-deploy phase (~10 min, operator-side)

A real client onboarding does NOT begin until these are checked off.

### Contractual prerequisites

- [ ] DPA (Data Processing Agreement) signed by the client and counter-signed by us
- [ ] SLA tier agreed in writing (response time, uptime target, support hours)
- [ ] Invoice issued / payment received per the agreed schedule

### Information we collect from the client

- [ ] Client primary contact email
- [ ] Client primary Telegram user ID (NOT username — the numeric ID; they can find it via [@userinfobot](https://t.me/userinfobot))
- [ ] Client GitHub username (only if they want custom skills delivered via PRs to the data repo)
- [ ] Anthropic plan tier they hold (Pro / Max / Team / Enterprise — affects which models the agent can call)
- [ ] Display name + persona description (1-2 paragraphs of what their agent should do)

### Internal record-keeping

- [ ] Open `bubble-vps-data/tenants/<name>/README.md` (created in the next phase by `new-tenant.sh`) and fill in:
  - Contract details — signed date, plan, SLA tier
  - Primary contact info
  - Billing info (invoice number, payment date)
  - Anthropic plan tier

---

## Provisioning phase (~15 min, mostly automated)

Run from `~/code/bubble-vps-platform`. The exact same command sequence as [INSTALL.md](INSTALL.md) §"Per-tenant happy path", just framed as a checklist.

- [ ] **Scaffold the tenant.** `./scripts/new-tenant.sh <name> --type=client --display-name="<Name>"` — creates `bubble-vps-data/tenants/<name>/{tenant.yaml, secrets.sops.env (encrypted, with placeholders), persona/, README.md}`
- [ ] **Edit tenant.yaml.** Fill the placeholders the script left:
  - `contact.primary_email`
  - `contact.primary_telegram_user_id` (the client's numeric ID, NOT yours)
  - `agent.channels.telegram.allowed_user_ids` (must include the client's ID; add yours too only if you want operator access during the trial period)
  - `agent.persona.display_name` and any persona-specific fields
- [ ] **Provision the box.** `./scripts/provision-tenant.sh <name>` — creates the Hetzner CX33 in fsn1, attaches the firewall + your SSH key, waits for SSH to become reachable, writes `host.ip` back into `tenant.yaml`, commits to the data repo
- [ ] **Set the 5 required secrets** (each opens a native GUI password prompt — values never echo to the terminal):
  - `./scripts/operator-set-secret.sh --tenant=<name> --key=TELEGRAM_BOT_TOKEN`
  - `./scripts/operator-set-secret.sh --tenant=<name> --key=CLAUDE_CODE_OAUTH_TOKEN`
  - `./scripts/operator-set-secret.sh --tenant=<name> --key=TAILSCALE_AUTHKEY`
  - `./scripts/operator-set-secret.sh --tenant=<name> --key=PHONEHOME_TOKEN`
  - `./scripts/operator-set-secret.sh --tenant=<name> --key=GITHUB_TOKEN`
- [ ] **First deploy.** `./scripts/deploy.sh --tenant=<name>` — installs hardening + age key on the box, prints the box's public age key, then HALTS at the Phase D first-half gate. This is by design (see [SPEC-006](../specs/SPEC-006-secrets-sops-age.md) and [SPEC-008](../specs/SPEC-008-secrets-deploy-second-half.md)).
- [ ] **Add box pubkey to `.sops.yaml` + re-encrypt.**
  ```bash
  $EDITOR ../bubble-vps-data/.sops.yaml
  cd ../bubble-vps-data
  SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops updatekeys --yes tenants/<name>/secrets.sops.env
  git commit -am "Step 3 bootstrap: add <name> box pubkey"
  cd -
  ```
- [ ] **Second deploy.** `./scripts/deploy.sh --tenant=<name>` — lands cleanly. Box decrypts its own secrets, agent starts, Tailscale joins, phone-home registers.

---

## Post-deploy verification (~5 min, manual smoke test)

- [ ] **Telegram smoke test.** Send "hello" to the client's bot; expect a reply within ~5s. If no reply: see [RUNBOOK.md](RUNBOOK.md) §"Telegram plugin not responding".
- [ ] **Dashboard check.** Open `http://{{VPS_HOST}}.{{TAILNET}}.ts.net:3848/` from any tailnet device. Expect a row for `<name>` showing green / heartbeat in the last 5 min / agent uptime / claude version. If missing: see [RUNBOOK.md](RUNBOOK.md) §"Dashboard not loading".
- [ ] **Security audit verification.** Either wait for tomorrow 09:00 UTC (the daily timer fires on its own — see [SPEC-014](../specs/SPEC-014-cloud-security-cron.md)) or trigger manually: `ssh <name>-vps 'sudo systemctl start bubble-security-audit.service'`. Confirm a report posts to `@ContentbubbleClawbot` (or the tenant's audit chat).
- [ ] **Tailscale device check.** `tailscale status | grep <name>` from operator Mac. Expect to see the box online with `tag:bubble-tenant`.
- [ ] **Drift check.** `./scripts/deploy.sh --tenant=<name>` a third time — should report 0 changes (idempotency proof).

---

## Welcome email (operator-side, ~3 min)

Send to the client primary email. Template:

```
Subject: Your Bubble VPS agent is live — <Display Name>

Hi <Client>,

Your agent is now operational. Quick reference:

  • Telegram bot: @<bot-username> — message it directly to interact
  • Anthropic plan: <tier>
  • SLA tier: <tier> (response within <X>h, <Y>% uptime target)

What we manage on your behalf:
  • Linux hardening (UFW, fail2ban, sshd, unattended-upgrades) — daily security
    audits run at 09:00 UTC and we monitor for drift
  • Secrets layer (SOPS+age encryption, no plaintext on disk)
  • Agent health (auto-restart, Telegram watchdog, central dashboard)

What you can do:
  • Talk to the agent via Telegram — it remembers context across sessions
  • Request changes by emailing this address (we'll deploy them within <SLA>h)
  • Anything urgent → reply with "URGENT" in the subject

Support: <support-email> | hours: <hours>

Best,
<Operator>
```

Note: the dashboard URL is operator-internal — do NOT include it in the welcome email unless the client is explicitly added to the tailnet.

---

## Total time

| Phase | Time |
|---|---|
| Pre-deploy (contracts + info gathering) | ~10 min |
| Provisioning (the 7 commands above) | ~15 min |
| Post-deploy verification | ~5 min |
| Welcome email | ~3 min |
| **Total** | **~33 min** |

This satisfies litmus criterion #2 from `proposals/end-state-vision.md` (onboard new client in <30 min — close enough; the long pole is Hetzner provisioning latency, not our scripts).
