# INSTALL — bubble-vps-platform

Full "0 → working tenant" walkthrough for an operator setting up a fresh Mac and bringing up the first tenant box. Targets the happy path; troubleshooting at the bottom.

For the architecture overview see [ARCHITECTURE.md](ARCHITECTURE.md). For the per-client checklist see [ONBOARDING.md](ONBOARDING.md). For the threat model see [SECURITY.md](SECURITY.md).

---

## Prerequisites

- macOS operator workstation (Linux works for most steps, but the operator-side scripts use `osascript` / Keychain by default — see SPEC-008 for the Linux fallback path with `gum`)
- Python 3.10 or newer
- `pipx` (`brew install pipx && pipx ensurepath`)
- `git`
- A Hetzner Cloud account with API access (used for box provisioning)
- A Tailscale account with `tagOwners["tag:bubble-tenant"]` granted to the operator (used for ops access to tenant boxes)
- Optional but strongly recommended: FileVault enabled on the operator Mac (the master age key lives on the disk; Keychain + FileVault is the root of trust — see [SECURITY.md](SECURITY.md))

---

## One-time operator setup

### 1. Install pyinfra

```bash
pipx install pyinfra
pyinfra --version    # expect 3.8 or newer
```

### 2. Install SOPS + age

```bash
brew install sops age
sops --version       # expect 3.x
age --version
```

### 3. Bootstrap the master age keypair

```bash
cd ~/code/bubble-vps-platform        # or wherever you cloned this repo
./scripts/operator-bootstrap-age.sh
```

The script:

- Generates `~/.config/sops/age/keys.txt` with mode 0600 if it does not exist
- Prints the PUBLIC key (starts with `age1...`) — paste this into `bubble-vps-data/.sops.yaml` under the recipient list for each tenant you can decrypt for
- Refuses to clobber an existing key (the file is the root of trust — losing it means losing the ability to decrypt every tenant's secrets that have you as a recipient)

### 4. Install the Hetzner CLI + put the API token in Keychain

```bash
brew install hcloud
# Get a Read+Write API token from https://console.hetzner.cloud/projects → your project → Security → API Tokens
security add-generic-password -s "hetzner-cloud" -a api_token -w "<paste-token>"
```

`scripts/provision-tenant.sh` reads this entry — it never echoes the token, never writes it to disk outside Keychain.

### 5. Install + auth Tailscale on the operator Mac

```bash
brew install --cask tailscale
open -a Tailscale
# Sign in via the GUI; verify `tailscale status` shows your Mac with `tag:` (no tag is fine on the operator Mac itself)
```

In the Tailscale admin UI ([login.tailscale.com/admin/acls](https://login.tailscale.com/admin/acls)) ensure:

- `tagOwners` includes `"tag:bubble-tenant": ["<your-tailnet-email>"]`
- An auth key is generated as **reusable + pre-approved + tagged with `tag:bubble-tenant`** at [admin/settings/keys](https://login.tailscale.com/admin/settings/keys). You will paste this per tenant in step "Per-tenant happy path" below.

See [SPEC-011](../specs/SPEC-011-tailscale.md) for the rationale on tag scoping.

### 6. Clone both repos as siblings

```bash
mkdir -p ~/code && cd ~/code
git clone <bubble-vps-platform-url> bubble-vps-platform
git clone <bubble-vps-data-url>     bubble-vps-data
```

The platform expects `bubble-vps-data` at `../bubble-vps-data` relative to itself. Override with `BUBBLE_DATA_REPO=/abs/path` if you keep them elsewhere.

### 7. Create the project venv (for tests + lint)

```bash
cd ~/code/bubble-vps-platform
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m pytest lib/   # expect 204 / 204 passing
```

---

## Per-tenant happy path

This is the exact sequence to take a new tenant from zero to first deploy. Each command is idempotent unless noted.

```bash
cd ~/code/bubble-vps-platform

# 1. Scaffold the tenant in the data repo (creates tenant.yaml, persona/, README, encrypted secrets file with placeholders)
./scripts/new-tenant.sh acme-corp --type=client --display-name="Acme Corp"

# 2. Edit the placeholders the script left in tenant.yaml
#    (contact.primary_email, contact.primary_telegram_user_id, agent.channels.telegram.allowed_user_ids, etc.)
$EDITOR ../bubble-vps-data/tenants/acme-corp/tenant.yaml

# 3. Provision the Hetzner box (CX33 in fsn1, Ubuntu 24.04, attached to bubble-default firewall, your SSH key)
./scripts/provision-tenant.sh acme-corp
#    -> Updates host.ip in tenant.yaml automatically once SSH is reachable
#    -> Commits the change to the data repo

# 4. Set the 5 required secrets via the GUI prompt (no terminal echo of values)
./scripts/operator-set-secret.sh --tenant=acme-corp --key=TELEGRAM_BOT_TOKEN
./scripts/operator-set-secret.sh --tenant=acme-corp --key=CLAUDE_CODE_OAUTH_TOKEN
./scripts/operator-set-secret.sh --tenant=acme-corp --key=TAILSCALE_AUTHKEY
./scripts/operator-set-secret.sh --tenant=acme-corp --key=PHONEHOME_TOKEN
./scripts/operator-set-secret.sh --tenant=acme-corp --key=GITHUB_TOKEN

# 5. First deploy — INTENTIONALLY HALTS at Phase D first-half gate
./scripts/deploy.sh --tenant=acme-corp
#    The deploy installs hardening + age key on the box, then prints the box's
#    PUBLIC age key and stops. This is the bootstrap gate from SPEC-006/008.

# 6. Add the box's pubkey to .sops.yaml as a second recipient + re-encrypt
$EDITOR ../bubble-vps-data/.sops.yaml
#    Add the box pubkey under the tenants/acme-corp/secrets.sops.env entry
cd ../bubble-vps-data
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops updatekeys --yes tenants/acme-corp/secrets.sops.env
git add .sops.yaml tenants/acme-corp/secrets.sops.env
git commit -m "Step 3 bootstrap: add acme-corp box pubkey to .sops.yaml"
cd -

# 7. Second deploy — lands cleanly (box can now decrypt its own secrets, agent service starts)
./scripts/deploy.sh --tenant=acme-corp

# 8. Smoke test
#    Send "hello" to the tenant's Telegram bot — expect a reply within ~5s.
#    Open http://{{VPS_HOST}}.{{TAILNET}}.ts.net:3848/ — expect a row for acme-corp
#    showing green / heartbeat / agent uptime.
```

Total time end-to-end: ~30 min, mostly waiting on Hetzner provisioning (3-4 min) and the deploy itself (~8 min). See [ONBOARDING.md](ONBOARDING.md) for the same sequence wrapped in operator-checklist semantics (DPA verification, welcome email, etc.).

---

## Dry runs

```bash
./scripts/deploy.sh --tenant=acme-corp --dry-run
```

Parses tenant.yaml, plans the operations, but does not connect to the box. Useful for validating a tenant.yaml change before applying it.

---

## Verifying the platform itself

The drift-test runbook ([RUNBOOK.md](RUNBOOK.md) §"How to verify the hardening playbook is healthy") is the litmus: run the hardening task twice against `bubble-internal`; both runs must report `Changed: 0`. See that doc for the full procedure and the negative-test drill.

---

## Troubleshooting

### `TENANT environment variable required`

You ran `pyinfra` directly without setting `TENANT` or using `scripts/deploy.sh`. Use the wrapper.

### `BUBBLE_DATA_REPO does not exist`

Clone the data repo as a sibling of the platform repo, or export `BUBBLE_DATA_REPO=/path/to/bubble-vps-data`.

### SSH `Permission denied (publickey)`

The deploy uses key-based SSH from the operator Mac to `<ssh_user>@<host.ip>`. Verify:

```bash
ssh hetzner 'whoami; hostname'
# Expect: claude / <tenant-vps>
```

If that works, pyinfra should too. If not, check `~/.ssh/config` has the host alias mapping to the right user/IP, and that the SSH key attached during `provision-tenant.sh` matches a key in `~/.ssh/`.

### `Connection refused` / `Connection timed out` after rapid re-runs

UFW limits SSH to 6 connections per 30s per source IP (see [SPEC-004](../specs/SPEC-004-ssh-rate-limit-policy.md)). Wait ~30s, retry. The wrapper retries 2x with 5s backoff by default. Once Tailscale is up (after the first successful deploy), prefer `ssh <tenant>-vps` over the public IP — that bypasses UFW's rate-limit since traffic comes via the Tailscale interface.

### Deploy fails at `sops verify` / "could not decrypt"

The box cannot decrypt `/etc/bubble/secrets.sops.env` with its own age key. Causes:

- The box pubkey is not yet in `.sops.yaml` (you skipped step 6 of the happy path) → add it + run `sops updatekeys` + redeploy
- `.sops.yaml` was edited but `sops updatekeys` was not run → the encrypted file still has the old recipient list → run `sops updatekeys --yes <file>`
- `/etc/age/key.txt` on the box was regenerated → the recipient list in `.sops.yaml` references the old pubkey → reset to the new pubkey and re-run `updatekeys`

See [RUNBOOK.md](RUNBOOK.md) §"Deploy fails at sops verify" for the full diagnostic flow.

### `pyinfra reports N > 0 changes against a clean box`

You have drift. Either someone manually edited the box, or the playbook itself drifted. See [RUNBOOK.md](RUNBOOK.md) §"How to verify the hardening playbook is healthy" → §"What 'changed' means".

### Tenant.yaml validation errors

The loader (`lib/tenant_loader.py`) enforces the SPEC-001 schema. Common errors:

- Tenant name not matching `^[a-z][a-z0-9-]*$` → fix the directory name + the `tenant.name` field
- Missing `host.ip` → fill it in (or run `provision-tenant.sh` to get one)
- Missing `secrets.required_keys` entry that is set in the env file → either add it to required_keys or remove from the env file

Run `.venv/bin/python -m pytest lib/test_tenant_loader.py -v` to see all schema rules with examples.
