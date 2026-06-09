# OFFBOARDING — tenant offboarding playbook

Companion to [ONBOARDING.md](ONBOARDING.md). Two scenarios per [SPEC-018](../specs/SPEC-018-offboarding.md):

- **Scenario A — Handoff.** Client keeps the box; we lose all access (SSH, secrets, deploy).
- **Scenario B — Destroy.** Contract is over and the client wants nothing kept; box is destroyed, data archived for our audit trail.

Both flows use the single script `scripts/offboard-tenant.sh`. Both are designed so re-running on an already-offboarded tenant is safe.

---

## Pre-offboard phase (operator-side)

- [ ] Confirm in writing the client's choice (handoff vs destroy)
- [ ] Verify any final invoice has been sent / paid per contract terms
- [ ] If handoff: confirm the client has a working SSH key on the box (the one they originally provided during onboarding) and knows where their `/etc/age/key.txt` is for SOPS recovery
- [ ] If destroy: confirm the client does not need any data exported (transcripts, agent memory, custom skills) — once destroyed, this is irreversible

---

## Scenario A — Handoff

The box stays running for the client; we wipe our access.

### What the script does

`./scripts/offboard-tenant.sh <name> --mode=handoff`

1. Validates the tenant exists in `bubble-vps-data/tenants/<name>/`
2. Prints a summary and asks for confirmation (skip with `--yes` for scripted runs)
3. Removes the operator master pubkey from the tenant's recipient block in `.sops.yaml`
4. Runs `sops updatekeys --yes tenants/<name>/secrets.sops.env` so the encrypted file is re-encrypted with only the box pubkey as recipient — we can no longer decrypt
5. Prints the manual Tailscale step (programmatic device removal needs an API token; deferred to v2 per SPEC-018)
6. Archives `bubble-vps-data/tenants/<name>/` to `bubble-vps-data/tenants/_archive/<name>-handoff-<date>/`
7. Prints the client communication template (operator pastes into email)

### Commands

```bash
cd ~/code/bubble-vps-platform
./scripts/offboard-tenant.sh acme-corp --mode=handoff
# Read the printed summary, type 'yes' to confirm

# Manual step the script reminds you about:
#   Open https://login.tailscale.com/admin/machines
#   Find acme-corp-vps and click "Remove device"

# Commit the archive + .sops.yaml change in the data repo
cd ../bubble-vps-data
git add .sops.yaml tenants/_archive/acme-corp-handoff-<date>/
git rm -r --cached tenants/acme-corp/   # already moved by the script
git commit -m "Offboard acme-corp (handoff)"
```

### Client communication template

The script prints this verbatim — operator copies and emails to the client:

```
─────────────── CLIENT HANDOFF ───────────────

Dear <Client>,

Your Bubble VPS is now operating standalone. As of <date>, Bubble Invest no
longer has access to:
   • SSH (Tailscale device removed from our tailnet)
   • Secrets (our master age key removed from your SOPS recipients)
   • Configuration changes (we cannot deploy updates from our side)

Your box continues to run normally. Your data + secrets remain intact and
encrypted with your box's age key (located on the box at /etc/age/key.txt
— back it up).

Going forward:
   • You retain full root access via your SSH key (the one you provided)
   • Your secrets are decryptable only with /etc/age/key.txt — back it up
     to a secure location (USB key, password manager, etc.)
   • Updates: clone the open-source bubble-vps-platform repo, run pyinfra
     deploy yourself
   • Support: 30 days transition support included — reply to this email

Best,
Bubble Invest

──────────────────────────────────────────────
```

---

## Scenario B — Destroy

The box is destroyed, data archived for our audit trail only.

### What the script does

`./scripts/offboard-tenant.sh <name> --mode=destroy`

1. Validates tenant exists
2. Prints destruction summary
3. Asks for **typed confirmation** — operator must type the tenant name exactly (prevents accidents from `--yes` muscle memory or misclick)
4. Reads `host.provider_server_id` from `tenant.yaml`
5. Calls `hcloud server delete <server-id>` — destroys the Hetzner VM (snapshots also deleted; retention period is 0 unless the operator explicitly created persistent snapshots in the console)
6. Reminds operator to remove the Tailscale device manually (same as handoff)
7. Archives `bubble-vps-data/tenants/<name>/` to `bubble-vps-data/tenants/_archive/<name>-destroyed-<date>/`
8. Prints destruction confirmation summary

### Commands

```bash
cd ~/code/bubble-vps-platform
./scripts/offboard-tenant.sh acme-corp --mode=destroy
# Read the destruction summary, type 'acme-corp' (the tenant name) to confirm

# Manual step:
#   Open https://login.tailscale.com/admin/machines
#   Find acme-corp-vps and click "Remove device"

# Commit the archive
cd ../bubble-vps-data
git add tenants/_archive/acme-corp-destroyed-<date>/
git rm -r --cached tenants/acme-corp/   # already moved
git commit -m "Offboard acme-corp (destroyed)"
```

### Destruction confirmation email (optional, recommended)

Send to the client primary email:

```
Subject: <Display Name> — destruction confirmation

Hi <Client>,

Confirming the destruction of your Bubble VPS environment as of <date>.

  • Hetzner server <server-id> deleted (snapshots retention: 0)
  • Tailscale device removed from our tailnet
  • Tenant data archived in our internal records (under the audit-trail
    retention policy in your DPA, section <X>)

If you need anything else (e.g. a final export of agent transcripts that
we may have retained for support purposes), reply within 7 days. After
that, all retained data is irrecoverable.

Thank you for working with us.

Best,
Bubble Invest
```

---

## Idempotency

Per SPEC-018:

- Re-running on an already-archived tenant errors with a clear message ("tenant <name> not found in tenants/, did you mean tenants/_archive/?"). Safe — exits cleanly without touching anything.
- Re-running destroy on an already-deleted Hetzner server: hcloud returns 404, the script logs and continues. The end state is the goal, not the operation.

---

## Rollback (handoff scenario)

If a handoff was triggered by mistake before sending the client communication:

1. `git -C ../bubble-vps-data revert <commit-sha>` to restore the tenant directory and `.sops.yaml`
2. Re-run `sops updatekeys --yes tenants/<name>/secrets.sops.env` to add the operator pubkey back to the recipient list
3. Re-add the device in Tailscale admin (the box itself still has the auth key cached and will reconnect on next `tailscale up`)

There is **no rollback** for destroy mode. Once `hcloud server delete` runs, the box is gone and snapshots (if not explicitly persisted) are also gone within Hetzner's standard retention window.

---

## Audit trail

The archive directory under `bubble-vps-data/tenants/_archive/<name>-{handoff,destroyed}-<date>/` is committed to git. This is the durable record per the DPA — it includes the original tenant.yaml, README.md (contract details), and the encrypted secrets blob (which we can no longer decrypt after handoff but which proves what was deployed).

Do not delete archives — the retention policy lives in the DPA, not in this repo.
