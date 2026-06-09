# SPEC-002 — Inventory and Deploy entrypoint

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending {{OPERATOR}} approval_
**Depends on:** SPEC-001 (tenant.yaml schema)

---

## Purpose

Define how pyinfra discovers tenants, picks one (or all), connects to their hosts, and runs deploy operations. This is the entry point of every deploy command and must be:

1. **Reusable** — same code works for {{VPS_HOST}} and any future client box, distinguished only by tenant.yaml content.
2. **Safe** — explicit `--tenant=<name>` (or `--all`) required. No accidental "deploy to everything by default" footgun.
3. **Validated** — refuses to start if any tenant.yaml is malformed.
4. **Inspectable** — `--dry-run` / `--list-hosts` / `--list-operations` work without touching boxes.

---

## CLI surface

The platform exposes ONE `pyinfra` invocation pattern:

```bash
# Single tenant
pyinfra inventory.py --tenant=bubble-internal deploy.py

# All tenants
pyinfra inventory.py --tenants=all deploy.py

# Specific operations only (skip the full deploy.py orchestration)
pyinfra inventory.py --tenant=bubble-internal pyinfra/tasks/hardening/linux.py

# Dry run
pyinfra inventory.py --tenant=bubble-internal --dry-run deploy.py
```

`--tenant=` and `--tenants=all` are passed via pyinfra's `--data` mechanism (or a small wrapper). They are read from environment variables `TENANT` / `TENANTS_ALL=1` inside `inventory.py` for v1 simplicity. v2 may wrap pyinfra in a click CLI for nicer UX.

For v1, the convention is:

```bash
TENANT=bubble-internal pyinfra inventory.py deploy.py
```

A thin wrapper script `scripts/deploy.sh` will provide the friendlier `--tenant=` syntax.

---

## File: `inventory.py`

Located at the repo root.

**Responsibilities:**

1. Read `BUBBLE_DATA_REPO` env var (default: `../bubble-vps-data` relative to platform repo). Refuses to run if path doesn't exist.
2. Read `TENANT` env var (single) OR `TENANTS_ALL=1` (all). Refuses to run if neither set.
3. For each selected tenant:
   - Load `tenants/<name>/tenant.yaml`
   - Validate against SPEC-001 rules
   - Build a pyinfra host tuple: `(hostname, {ssh_user, ssh_port, ssh_hostname, ...host facts})`
   - Attach the loaded tenant config as `host.data.tenant` so deploy.py can access it
4. Expose the standard pyinfra inventory groups:
   - `linux_hosts` — all hosts where `os_family == linux`
   - `macos_hosts` — all hosts where `os_family == macos` (Phase 2)
   - `internal_hosts` — `tenant_type == internal`
   - `client_hosts` — `tenant_type == client`

**Key facts attached to each host:**

```python
host.data.tenant_name        # str
host.data.tenant_type        # str: internal | client
host.data.tenant_config      # full parsed tenant.yaml dict
host.data.persona_dir        # absolute path to persona dir on operator's Mac
host.data.secrets_file       # absolute path to secrets.sops.env
```

These are consumed by every task module.

---

## File: `deploy.py`

Located at the repo root.

**v1 behavior (Step 1 — hello world):**

For Step 1 (today), `deploy.py` does NOTHING destructive. It runs ONE pyinfra `server.shell` operation that prints a hello message, plus pyinfra's built-in `host.fact` lookups to verify connectivity.

Pseudo-code:

```python
from pyinfra import host
from pyinfra.operations import server

server.shell(
    name="Hello, tenant",
    commands=[
        f"echo '🟢 Connected to tenant: {host.data.tenant_name}'",
        f"echo 'Hostname: $(hostname)'",
        f"echo 'OS: $(uname -a)'",
        f"echo 'User: $(whoami)'",
    ],
)
```

**Verification criteria (Step 1 done when):**

1. ✅ `pyinfra inventory.py deploy.py` with `TENANT=bubble-internal` connects to {{VPS_HOST}}.
2. ✅ Output shows `🟢 Connected to tenant: bubble-internal`.
3. ✅ Hostname reads `{{VPS_HOST}}`.
4. ✅ User reads `claude`.
5. ✅ Exit code 0.
6. ✅ No changes to the box (only `server.shell` reads, no installs/edits).
7. ✅ Re-running gives identical output (idempotent — proves `server.shell` doesn't mutate state).

**Future v1 (Steps 2–6):**

`deploy.py` becomes an orchestrator that calls task modules in order:

```python
from pyinfra_tasks.hardening import linux as hardening
from pyinfra_tasks.secrets import age_setup, sops_decrypt
from pyinfra_tasks.agent import install, systemd, persona
from pyinfra_tasks.access import tailscale, phone_home

# Phase 1 — hardening
hardening.deploy()

# Phase 2 — secrets
age_setup.deploy()

# Phase 3 — agent install
install.deploy()
systemd.deploy()
persona.deploy()

# Phase 4 — access
tailscale.deploy()
phone_home.deploy()
```

Each task module validates its own preconditions and is idempotent.

---

## Validation pipeline

Before pyinfra connects to any host, `inventory.py` runs `lib/tenant_loader.py:validate_all_tenants()` which:

1. Iterates `tenants/<name>/tenant.yaml` for the requested tenant(s)
2. Parses YAML
3. Validates required fields, enums, secret refs, persona_dir existence
4. Returns a list of validated `TenantConfig` objects OR raises with specific error

If validation fails, `inventory.py` raises and pyinfra exits before any SSH connection.

This is the **fail-fast** pattern that prevents partial deploys from broken configs.

---

## Test plan (Step 1)

These tests must pass before declaring Step 1 done:

### T1. Skeleton structure exists
```bash
test -f bubble-vps-platform/inventory.py
test -f bubble-vps-platform/deploy.py
test -f bubble-vps-platform/lib/tenant_loader.py
test -d bubble-vps-data/tenants/bubble-internal
test -f bubble-vps-data/tenants/bubble-internal/tenant.yaml
```

### T2. tenant.yaml validates
```bash
cd bubble-vps-platform
python3 -c "from lib.tenant_loader import load_tenant; t = load_tenant('bubble-internal'); print(t.tenant_name)"
# Expected output: bubble-internal
```

### T3. Invalid tenant.yaml fails fast
```bash
# Create a deliberately-broken tenant.yaml in a temp dir
echo "tenant_name: wrong-name" > /tmp/broken.yaml
python3 -c "from lib.tenant_loader import load_tenant_from_path; load_tenant_from_path('/tmp/broken.yaml', expected_name='broken')" || echo "PASS: rejected"
```

### T4. inventory.py dry-run shows expected hosts
```bash
TENANT=bubble-internal pyinfra inventory.py --dry-run deploy.py
# Expected: shows {{VPS_HOST}} in inventory, exits 0 without connecting
```

### T5. End-to-end hello world
```bash
TENANT=bubble-internal pyinfra inventory.py deploy.py
# Expected output includes:
#   🟢 Connected to tenant: bubble-internal
#   Hostname: {{VPS_HOST}}
#   User: claude
# Exit code: 0
```

### T6. Idempotency (re-run = no diff)
```bash
TENANT=bubble-internal pyinfra inventory.py deploy.py 2>&1 | tee /tmp/run1.log
TENANT=bubble-internal pyinfra inventory.py deploy.py 2>&1 | tee /tmp/run2.log
# Expected: pyinfra "changes" count is 0 in both runs (server.shell echoes don't count as changes in pyinfra's reporting)
```

### T7. Refuses to run without TENANT
```bash
pyinfra inventory.py deploy.py 2>&1 | grep -i "TENANT environment variable required"
# Expected: error message and non-zero exit
```

---

## Out of scope for Step 1

- Real hardening tasks (Step 2)
- SOPS+age secrets layer (Step 3)
- systemd unit / agent install (Step 4)
- Persona rsync (Step 5)
- Tailscale / phone-home (Step 6)
- Wrapper CLI (`scripts/deploy.sh`) — minimal version OK for Step 1

---

## Open questions (resolve before Step 2)

1. **pydantic vs dataclass for tenant validation?** Recommendation: dataclass + manual validation for v1 (no extra dep). Switch to pydantic in v2 if validation gets complex.

2. **YAML library?** `pyyaml` is universal but slow; `ruamel.yaml` preserves comments. Recommendation: pyyaml for v1 (we don't round-trip comments).

3. **Where does `lib/` live?** Inside `bubble-vps-platform/lib/` so it ships with the platform repo. NOT inside `pyinfra/` — `lib/` is shared between inventory and tasks.

4. **Should `inventory.py` import from `lib/`?** Yes. pyinfra runs `inventory.py` as a regular Python file from the repo root; relative imports work.
