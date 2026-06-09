# SPEC-018 — Tenant offboarding playbook + script (Step 7c)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Step 7a/7b done; tenant has a working deploy
**Implements:** Step 7c of the Bubble VPS Platform build plan — closes litmus criterion #5 (offboard a client cleanly with no residual access)

---

## Purpose

Two scenarios:

**Scenario A — Hand the box to the client (they keep running it without us).**
Goal: remove ALL of our access while preserving their working state.
- Remove our age public key from `.sops.yaml` (we can no longer read their secrets)
- Remove the box from our Tailscale tailnet
- Document for client: "your box is now standalone. Here's the SOPS recovery info, here's how to update settings.json yourself, here's our support contact for the next 30 days as part of offboarding."
- Archive the tenant entry in our data repo (don't delete — keep audit trail)

**Scenario B — Destroy the box (contract over, client wants nothing kept).**
Goal: provably destroy data + infrastructure.
- `hcloud server delete <name>-vps`
- Remove from Tailscale admin
- Archive tenant data repo entry
- Optional: send "destruction confirmation" report to client (date, server ID, snapshot retention period 0)

---

## CLI

```bash
./scripts/offboard-tenant.sh <tenant-name> [--mode=handoff|destroy] [--yes]
```

Required:
- `<tenant-name>` — must match an existing tenant in the data repo

Optional flags:
- `--mode=handoff|destroy` — default `handoff`
- `--yes` — skip interactive confirmations (dangerous — only for scripts)

---

## Operations (mode=handoff)

```bash
# 1. Validate tenant exists
test -d bubble-vps-data/tenants/<name>/ || exit 2

# 2. Show summary + ask for confirmation (unless --yes)
echo "About to OFFBOARD tenant: <name>"
echo "  Mode: handoff (box stays running, our access removed)"
echo "  Actions:"
echo "    - Remove operator master pubkey from .sops.yaml"
echo "    - sops updatekeys (re-encrypt with box pubkey only)"
echo "    - Remove box from our Tailscale admin (manual, link printed)"
echo "    - Move tenants/<name>/ to tenants/_archive/<name>-handoff-<date>/"
echo "    - Print client handoff doc"
read -p "Proceed? (yes/no): " confirm
[[ "$confirm" != "yes" ]] && { echo "aborted"; exit 0; }

# 3. Modify .sops.yaml: remove operator master pubkey from this tenant's recipient block
python3 -c "
import re
path = 'bubble-vps-data/.sops.yaml'
content = open(path).read()
# In the bubble-internal-style entry, find the age: list and remove the operator master pubkey line
# (this is fiddly — uses the operator master pubkey as a needle)
operator_master = 'age1qal34hv5h99vvpq7kmghfz0mjh98eq9mj5dg5k43r8kwmumvnu5qt6w3hy'
# Find section for this tenant
# ... regex magic to remove the operator key from the right block
"

# 4. sops updatekeys to re-encrypt with the new (smaller) recipient list
cd bubble-vps-data
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops updatekeys --yes tenants/<name>/secrets.sops.env

# 5. Manual step (printed): operator must remove the box from Tailscale admin via web UI
echo "⚠ MANUAL STEP REQUIRED:"
echo "   Open https://login.tailscale.com/admin/machines"
echo "   Find <name>-vps and click 'Remove device'"
echo "   (Programmatic removal requires Tailscale API token; deferring to v2)"

# 6. Archive the tenant directory
DATE=$(date -u +%Y-%m-%d)
mkdir -p bubble-vps-data/tenants/_archive
mv bubble-vps-data/tenants/<name> bubble-vps-data/tenants/_archive/<name>-handoff-$DATE/

# 7. Print client handoff doc to stdout (operator copies + emails to client)
cat <<EOF
─────────────── CLIENT HANDOFF ───────────────

Dear <client>,

Your Bubble VPS is now operating standalone. As of $DATE, Bubble Invest no
longer has access to:
   • SSH (Tailscale device removed from our tailnet)
   • Secrets (our master age key removed from your SOPS recipients)
   • Configuration changes (we cannot deploy updates from our side)

Your box continues to run normally. Your data + secrets remain intact and
encrypted with your box's age key (located on the box at /etc/age/key.txt
— back it up).

Going forward:
   • You retain full root access via your SSH key (the one you provided)
   • Your secrets are decryptable only with /etc/age/key.txt — back it up to
     a secure location (USB key, password manager, etc.)
   • Updates: clone the open-source bubble-vps-platform repo, run pyinfra
     deploy yourself
   • Support: 30 days transition support included — reply to this email

Best,
Bubble Invest

──────────────────────────────────────────────
EOF
```

---

## Operations (mode=destroy)

```bash
# Same steps 1-2 as handoff, but with destroy-mode messaging
# Then:

# 3. Confirm destruction (extra prompt — irreversible)
read -p "Type the tenant name to confirm DESTRUCTION: " confirm
[[ "$confirm" != "<name>" ]] && { echo "aborted — name didn't match"; exit 0; }

# 4. Destroy Hetzner server
SERVER_ID=$(yq '.host.provider_server_id' bubble-vps-data/tenants/<name>/tenant.yaml)
hcloud server delete $SERVER_ID

# 5. Manual: Tailscale admin (same as handoff)

# 6. Archive
DATE=$(date -u +%Y-%m-%d)
mkdir -p bubble-vps-data/tenants/_archive
mv bubble-vps-data/tenants/<name> bubble-vps-data/tenants/_archive/<name>-destroyed-$DATE/

# 7. Print destruction confirmation
echo "✅ Tenant destroyed. Server $SERVER_ID deleted from Hetzner. Tenant data archived."
echo ""
echo "Manual: remove the Tailscale device, send the destruction confirmation to client."
```

---

## SPEC-008 hard rule compliance

- HCLOUD_TOKEN read from Keychain via `security find-generic-password -w`, captured into env var, never echoed
- The destruction prompt asks for the tenant NAME (not a token), prevents accidents
- The handoff client-doc lists what we DO have access to anymore, but NEVER includes any secret values

---

## Idempotency

- Re-running on an already-archived tenant: errors with "tenant <name> not found in tenants/, did you mean tenants/_archive/?". Safe.
- Re-running on a deleted Hetzner server: hcloud returns 404, script logs and continues (the goal is achieved).

---

## Test plan

### Static tests in `lib/test_offboard_tenant_script.py`

1. `test_script_exists_and_executable`
2. `test_script_rejects_no_args`
3. `test_script_rejects_missing_tenant`
4. `test_script_handoff_mode_calls_sops_updatekeys` — assert script source contains `sops updatekeys`
5. `test_script_destroy_mode_calls_hcloud_server_delete` — assert script source contains `hcloud server delete`
6. `test_script_archives_tenant_dir` — assert script moves to `tenants/_archive/`
7. `test_script_destroy_requires_typed_confirmation` — assert script reads tenant name as confirmation in destroy mode

### Integration test (NOT automated — destroys real data)

Manual:
```bash
# Test on a throwaway tenant — DO NOT use bubble-internal!
./scripts/new-tenant.sh test-offboard --type=client --display-name="Test Offboard"
./scripts/offboard-tenant.sh test-offboard --mode=handoff --yes
# Verify: bubble-vps-data/tenants/_archive/test-offboard-handoff-<date>/ exists
# Verify: bubble-vps-data/tenants/test-offboard does NOT exist
```

---

## Acceptance criteria

Step 7c done when:
1. ✅ `scripts/offboard-tenant.sh` exists, executable, 0755
2. ✅ Handoff mode: removes operator key from .sops.yaml, sops updatekeys, archives dir
3. ✅ Destroy mode: deletes Hetzner server, archives dir, requires typed confirmation
4. ✅ Both modes: prints clear next-steps for the manual Tailscale removal
5. ✅ 7 new static tests pass
6. ✅ All previous tests still pass (175 → 182)

---

## Out of scope

- Programmatic Tailscale device removal (requires Tailscale API token in Keychain — defer to v2 when we automate at-scale)
- Auto-emailing the client handoff doc (just prints to stdout — operator pastes into email)
- DPA/SLA cancellation paperwork (legal-side, Jade owns)
- Snapshot retention (Hetzner snapshots: separate, manual via console for now)
- Restoring an archived tenant back to active (not a flow we plan; if needed, manual `mv` from _archive back)
