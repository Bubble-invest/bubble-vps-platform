# SPEC-016 — `new-tenant.sh` bootstrapping script (Step 7a)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 1-6 done
**Implements:** Step 7a of the Bubble VPS Platform build plan — closes litmus criterion #2 (onboard new client in <30 min) by automating the 0-step of "create the tenant scaffolding"

---

## Purpose

Operator runs `./scripts/new-tenant.sh acme-corp` (or with flags), gets `bubble-vps-data/tenants/acme-corp/` populated with:

- `tenant.yaml` — pre-filled from a template, with placeholders for what only the operator can decide (IP, persona name, contact email)
- `secrets.sops.env` — encrypted with placeholders (`OPENROUTER_API_KEY=PASTE_HERE`, etc.)
- `persona/<name>/CLAUDE.md` — placeholder
- `README.md` — internal notes about the tenant

The script does NOT:
- Provision the Hetzner box (Step 7b's hetzner.py task does that — separate concern)
- Generate or paste secrets (operator handles via the existing `operator-set-secret.sh` GUI dialog)
- Deploy anything to a box (operator runs `pyinfra deploy --tenant=<name>` after filling in the tenant.yaml)

This is **scaffolding only** — it gets the operator from "deal closed" to "ready to fill in tenant-specific values" in 30 seconds.

---

## CLI

```bash
./scripts/new-tenant.sh <tenant-name> [--type=client|internal] [--display-name="Acme Corp"] [--persona=morty]
```

Required:
- `<tenant-name>` — positional. Lowercase, hyphens only (matches SPEC-001 `_TENANT_NAME_RE`). E.g. `acme-corp`.

Optional flags:
- `--type=client|internal` — default `client`
- `--display-name="..."` — default same as tenant-name with capitalized words
- `--persona=<name>` — default = tenant-name (so `acme-corp` → persona `acme-corp` by default)
- `--force` — overwrite existing tenant dir (otherwise refuses to clobber)

Errors:
- Tenant dir already exists (without --force): exit 2 with clear message
- Invalid tenant-name format: exit 2 with regex hint
- BUBBLE_DATA_REPO not found: exit 2 (same as inventory.py)

---

## Output structure

After `./scripts/new-tenant.sh acme-corp --type=client --display-name="Acme Corp"`:

```
bubble-vps-data/tenants/acme-corp/
├── tenant.yaml                # filled with placeholders + comments showing what to edit
├── secrets.sops.env           # encrypted, contains placeholder values for required keys
├── persona/
│   └── acme-corp/
│       ├── CLAUDE.md          # 1-line placeholder
│       └── workspace/
│           └── CLAUDE.md      # workspace-level placeholder
└── README.md                  # internal notes — contract details, contact, SLA TBD
```

---

## tenant.yaml template (jinja2 substitutions)

Uses an existing template at `pyinfra/templates/tenant.yaml.j2` (NEW — write as part of this task). Variables:

- `tenant_name` (from positional arg)
- `tenant_type` (from --type, default `client`)
- `display_name` (from --display-name, default Title-Cased tenant-name)
- `persona_name` (from --persona, default tenant-name)
- `created_at` (auto: today's date)
- `provisioned_by` (env: `$USER`)

Critical placeholders that MUST be edited before deploy (script comments them clearly):

```yaml
host:
  ip: PLACEHOLDER_FILL_AFTER_HETZNER_PROVISION
  hostname: PLACEHOLDER_USE_TENANT_NAME
  # ... etc
```

The placeholder strings are deliberately invalid (e.g. `PLACEHOLDER_FILL_AFTER_HETZNER_PROVISION` — not a valid IPv4) so the SPEC-001 validator catches them at deploy time. Failing closed > silently using a wrong default.

---

## secrets.sops.env initial creation

Reuses the same pattern from Step 3 Phase B — creates a plaintext file with placeholder values, encrypts in place. The placeholders match the new tenant's `required_keys` per the template.

Initial encrypted values (all placeholders):
```
TELEGRAM_BOT_TOKEN=PASTE_FROM_BOTFATHER
CLAUDE_CODE_OAUTH_TOKEN=PASTE_FROM_CLAUDE_SETUP_TOKEN
TAILSCALE_AUTHKEY=PASTE_FROM_TAILSCALE_ADMIN
PHONEHOME_TOKEN=GENERATE_VIA_OPENSSL_RAND_HEX_32
```

The script ENCRYPTS with the operator master key only (per `.sops.yaml` default rule that matches `tenants/[^/]+/secrets\.sops\.env$`). The per-tenant box pubkey gets added later, at Phase D first-half time, AFTER the box is provisioned and registered.

---

## SPEC-008 hard rule compliance

The script does NOT touch real secret values. The placeholders are literal strings. No GUI dialog, no interactive prompts. The encryption step uses `sops --encrypt --in-place` which doesn't echo plaintext.

After the operator runs `new-tenant.sh`, they invoke `operator-set-secret.sh --tenant=<name> --key=<KEY>` for each placeholder to paste real values via the GUI dialog (per SPEC-008 hard rule: secrets go from operator's keyboard to encrypted file, never via stdout).

---

## Idempotency

Running twice without `--force`: second run errors with "tenant dir already exists, use --force to overwrite". Safe.

With `--force`: overwrites all files. Operator is responsible for not running over a tenant that has real secrets. (Future hardening: refuse `--force` if secrets.sops.env contains non-placeholder values — but that requires decrypting to check, which reintroduces SPEC-008 stdout risk. Defer.)

---

## Test plan

### Static tests in `lib/test_new_tenant_script.py`

1. `test_script_exists_and_executable` — file exists, mode includes 0100
2. `test_script_rejects_no_args` — `new-tenant.sh` without args → exit 2
3. `test_script_rejects_invalid_tenant_name` — uppercase or special chars → exit 2 with regex hint
4. `test_script_creates_directory_structure` — run with a temp BUBBLE_DATA_REPO, assert all 4 expected files appear
5. `test_script_yaml_passes_loader_validation` — generated tenant.yaml passes through `lib.tenant_loader.load_tenant_from_path()` IF placeholders are filled (using a transformer that replaces PLACEHOLDER_* with valid values). Confirms the template generates a structurally valid skeleton.
6. `test_script_secrets_file_encrypted_with_correct_recipients` — generated secrets.sops.env has the operator master key as recipient (via `sops_age__list_0__map_recipient` field)
7. `test_script_refuses_to_clobber_without_force` — pre-create the dir, run without --force, expect exit 2
8. `test_script_overwrites_with_force` — pre-create, run with --force, expect success

### Manual integration test

```bash
./scripts/new-tenant.sh test-acme --type=client --display-name="Test Acme"
# Verify: bubble-vps-data/tenants/test-acme/ exists with all expected files
# Verify: SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops --decrypt tenants/test-acme/secrets.sops.env
#         shows the placeholder lines (no real values, no errors)
# Cleanup: rm -rf bubble-vps-data/tenants/test-acme
```

---

## Acceptance criteria

Step 7a done when:
1. ✅ `scripts/new-tenant.sh` exists, executable, 0755
2. ✅ Generates the 4 expected files for a new tenant
3. ✅ Generated tenant.yaml validates through `lib.tenant_loader` after placeholders filled
4. ✅ Generated secrets.sops.env decrypts cleanly with the operator master key
5. ✅ Refuses to clobber existing tenant dir without --force
6. ✅ 8 new static tests pass
7. ✅ All previous tests still pass (158 → 166)
8. ✅ Manual integration test produces a usable scaffold in <30 seconds

---

## Out of scope

- Hetzner provisioning (Step 7b)
- Pre-filling the box pubkey in `.sops.yaml` (happens at Phase D first-half deploy)
- Writing the README.md content beyond a stub (operator personalizes it)
- Generating a real persona (operator fills in `persona/<name>/CLAUDE.md` with a real agent definition, similar to how Morty was created from rnd.md)
- Multi-persona tenants (single persona per tenant for v1)
