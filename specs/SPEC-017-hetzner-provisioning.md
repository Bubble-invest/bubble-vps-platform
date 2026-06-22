# SPEC-017 — Hetzner box provisioning task (Step 7b)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Step 7a (new-tenant.sh) done; hcloud CLI installed on operator Mac
**Implements:** Step 7b of the Bubble VPS Platform build plan — closes litmus criterion #2 (onboard new client in <30 min) by automating "0 → SSH-able box ready for pyinfra deploy"

---

## Purpose

Bridge the gap between `new-tenant.sh` (creates the data-repo scaffolding) and `pyinfra deploy` (configures an existing reachable box). Right now, after running `new-tenant.sh acme-corp`, the operator must manually:
1. Open Hetzner Cloud Console
2. Create a server (CX33, Ubuntu 24.04, the right region)
3. Attach the right SSH key
4. Attach the firewall rule (only port 22 + ICMP from internet)
5. Wait for SSH to become reachable
6. Find the IP, copy it into `tenant.yaml` as `host.ip`
7. Run `pyinfra deploy --tenant=acme-corp`

That's ~10 minutes of clicking + manual updates. This spec automates steps 1-6 into ONE script: `scripts/provision-tenant.sh acme-corp`.

---

## CLI

```bash
./scripts/provision-tenant.sh <tenant-name> [--type=cx33] [--region=fsn1] [--image=ubuntu-24.04]
```

Required:
- `<tenant-name>` — must match an existing `tenants/<name>/tenant.yaml` in the data repo

Optional flags:
- `--type=<server-type>` — default `cx33`
- `--region=<location>` — default `fsn1` (Falkenstein, Germany — EU)
- `--image=<image>` — default `ubuntu-24.04`
- `--dry-run` — print what would happen, don't touch Hetzner

---

## Architecture

The script is a **bash wrapper around hcloud + python (for tenant.yaml editing) + pyinfra**. NOT a pyinfra task — pyinfra is for AFTER the box exists. This is BEFORE.

Why bash + hcloud, not pyinfra:
- pyinfra targets existing hosts via SSH. There's no host yet.
- hcloud has rich provisioning primitives (server create, firewall attach, ssh-key list)
- The "wait until SSH reachable" loop is a 10-line bash thing, no orchestration framework needed

---

## Operations

```bash
# 1. Validate tenant scaffolding exists
test -f bubble-vps-data/tenants/<name>/tenant.yaml || exit 2

# 2. Read hcloud token from macOS Keychain (already there from Hetzner migration project)
export HCLOUD_TOKEN=$(security find-generic-password -s "hetzner-cloud" -a api_token -w 2>/dev/null)
[ -z "$HCLOUD_TOKEN" ] && { echo "no hcloud token in keychain"; exit 2; }

# 3. List operator SSH keys, find one to attach (default: first key matching $BUBBLE_SSH_KEY_FILTER (default: operator-*))
SSH_KEY_ID=$(hcloud ssh-key list -o noheader -o columns=id,name | grep -i "${BUBBLE_OPERATOR_USER:-operator}" | head -1 | awk '{print $1}')

# 4. List firewalls, find bubble-default (already exists from Hetzner migration project)
FIREWALL_ID=$(hcloud firewall list -o noheader -o columns=id,name | grep "bubble-default" | head -1 | awk '{print $1}')

# 5. Provision server
hcloud server create \
    --type cx33 \
    --image ubuntu-24.04 \
    --location fsn1 \
    --name <tenant-name>-vps \
    --ssh-key $SSH_KEY_ID \
    --firewall $FIREWALL_ID \
    --label "tenant=<tenant-name>" \
    --label "managed-by=bubble-vps-platform"

# 6. Capture server ID + IP from response
SERVER_ID=$(hcloud server list -o noheader -o columns=id,name | grep "<tenant-name>-vps" | awk '{print $1}')
SERVER_IP=$(hcloud server describe $SERVER_ID -o json | jq -r .public_net.ipv4.ip)

# 7. Wait for SSH to become reachable (up to 5 min)
until ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@$SERVER_IP 'echo ready' 2>/dev/null | grep -q ready; do
    sleep 10
done

# 8. Bootstrap the non-root claude user (one-time, can't be done via pyinfra because pyinfra needs the user to exist already)
ssh root@$SERVER_IP 'useradd -m -s /bin/bash claude && usermod -aG sudo claude && echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude && mkdir -p /home/claude/.ssh && cp /root/.ssh/authorized_keys /home/claude/.ssh/ && chown -R claude:claude /home/claude/.ssh && chmod 700 /home/claude/.ssh && chmod 600 /home/claude/.ssh/authorized_keys'

# 9. Update tenant.yaml in-place: replace placeholders with real values
python3 -c "
import yaml
from pathlib import Path
y_path = Path('bubble-vps-data/tenants/<name>/tenant.yaml')
data = yaml.safe_load(y_path.read_text())
data['host']['ip'] = '$SERVER_IP'
data['host']['hostname'] = '<name>-vps'
data['host']['provider_server_id'] = '$SERVER_ID'
data['host']['region'] = 'fsn1-dc14'  # default, overridable
y_path.write_text(yaml.safe_dump(data, sort_keys=False))
"

# 10. Print success summary
echo "✅ Box provisioned. Next:"
echo "   1. operator-set-secret.sh --tenant=<name> --key=TELEGRAM_BOT_TOKEN  (etc for each required key)"
echo "   2. operator-bootstrap-age.sh   (if not done already, for the operator master key)"
echo "   3. ./scripts/deploy.sh --tenant=<name>"
echo "      ⚠ first deploy will halt at the box-pubkey-bootstrap gate (Phase D) —"
echo "      operator manually adds box pubkey to .sops.yaml + runs sops updatekeys, then re-deploys"
```

---

## SPEC-008 hard rule compliance

The script reads `HCLOUD_TOKEN` from Keychain via `security find-generic-password -w`. **CRITICAL:** the Keychain CLI's `-w` flag prints the value to stdout. We capture it into a shell variable via `$(...)` so the value is in stdout but goes into the variable, not visible in the terminal.

But `set -x` debugging would expose it. Mitigation: never enable -x. Also: `unset HCLOUD_TOKEN` at end of script (good hygiene though it dies with the process anyway).

The hcloud CLI uses `HCLOUD_TOKEN` env var natively. No `--token` arg needed. Good — the token never appears in `ps auxww`.

---

## Idempotency

If a server with the same name already exists, the script:
- DEFAULT: errors out with "server <name>-vps already exists, use --force to recreate"
- WITH `--force`: prompts the operator to confirm destruction (interactive; non-interactive `--force --yes` for scripts)

---

## What it does NOT do (deliberate scope boundary)

- **Does not deploy software** — that's pyinfra deploy's job. After this script, you have an Ubuntu box with the claude user + your SSH key + the firewall. Nothing bubble-specific is installed yet.
- **Does not create the SOPS box pubkey recipient entry** — that's Phase D first-half (pyinfra task `_age_setup` runs on first deploy, generates the box's age keypair, copies pubkey back to operator)
- **Does not generate the operator master age key** — that's `operator-bootstrap-age.sh`
- **Does not paste secrets** — operator runs `operator-set-secret.sh` for each
- **Does not provision Hetzner Cloud Firewall** — the firewall already exists (`bubble-default`, ID {{HETZNER_FIREWALL_ID}}, created during Hetzner migration project). Future tenants just attach to the existing one.

---

## Test plan

### Static tests in `lib/test_provision_tenant_script.py`

1. `test_script_exists_and_executable`
2. `test_script_rejects_missing_tenant` — calling with a tenant-name that has no scaffolding → exit 2
3. `test_script_rejects_no_args` → exit 2
4. `test_script_uses_hcloud_token_from_keychain` — assert script source contains `security find-generic-password -s "hetzner-cloud"`
5. `test_script_attaches_firewall` — assert script source includes `--firewall ` flag
6. `test_script_attaches_ssh_key` — assert script source includes `--ssh-key ` flag
7. `test_script_waits_for_ssh` — assert script has the until-loop pattern with `ssh ... 'echo ready'`
8. `test_script_updates_tenant_yaml_after_provision` — assert script has the python yaml-edit block
9. `test_script_does_not_print_token_to_stdout` — assert no `echo $HCLOUD_TOKEN` or similar leak

### Integration test (NOT automated — costs real money to provision a Hetzner box)

Manual when needed:
```bash
./scripts/new-tenant.sh test-provision --type=client --display-name="Test Provision"
./scripts/provision-tenant.sh test-provision --dry-run     # safety check first
./scripts/provision-tenant.sh test-provision               # actually provisions
# Verify: hcloud server list shows test-provision-vps
# Verify: ssh claude@<ip> 'echo hello' works
# Cleanup: hcloud server delete test-provision-vps && rm -rf bubble-vps-data/tenants/test-provision/
```

NOT part of the regular pytest run. Documented as an operator manual test.

---

## Acceptance criteria

Step 7b done when:
1. ✅ `scripts/provision-tenant.sh` exists, executable, 0755
2. ✅ Reads HCLOUD_TOKEN from macOS Keychain (already populated from Hetzner migration project)
3. ✅ Creates Hetzner server with right type/image/region/firewall/ssh-key
4. ✅ Waits for SSH to become reachable
5. ✅ Bootstraps the non-root claude user with NOPASSWD sudo
6. ✅ Updates the tenant's tenant.yaml in-place with real IP/hostname/server-id/region
7. ✅ 9 new static tests pass
8. ✅ All previous tests still pass (166 → 175)

---

## Out of scope

- Multi-region failover
- Hetzner Cloud Firewall creation (use existing `bubble-default`, ID {{HETZNER_FIREWALL_ID}})
- Custom server types beyond cx33 (overridable via flag, but defaults are right for our scale)
- Image alternatives beyond Ubuntu 24.04 (overridable via flag, but only this is tested)
- Non-Hetzner providers (AWS/GCP/Azure — would be a separate `provision-aws.sh`, etc.)
- Auto-deletion on tenant offboarding (Step 7c handles that)
