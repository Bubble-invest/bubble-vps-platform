# SPEC-003 — Host data exposure policy

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending {{OPERATOR}} approval_
**Fixes:** Step 1 Finding #2 (full tenant.yaml leaks into pyinfra host.data, can appear in shared logs)

---

## Problem

In Step 1, `inventory.py` attached the full parsed `tenant.yaml` dict (`cfg.raw`) to every host's `host.data.tenant_config`. pyinfra's `--debug` mode and crash reports can dump `host.data` into shared output (terminal, log files, our future central dashboard).

The dict contains per-tenant **non-secret-but-private** info:
- `contact.primary_email`
- `contact.primary_telegram_user_id`
- `host.provider_server_id` (Hetzner ID — infrastructure metadata)
- All `*_secret_ref` keys (env-var labels — not values, but still client-relationship metadata)
- `notes` (free-text per-tenant comments)

In a multi-tenant deploy (`TENANTS_ALL=1`), one debug dump could leak Tenant A's contact info into Tenant B's deploy log.

No actual SECRETS leak — but the cross-tenant privacy boundary is too loose.

## Threat model

- **Attacker who reads logs from a shared place** (CI, central dashboard, support ticket attachment): sees every other tenant's contact + infra metadata
- **Operator who screenshots terminal for help**: same thing
- **Tenant A reading their own logs**: sees Tenant B's data inadvertently

We are not trying to defend against a malicious operator with shell on the platform repo — they own the data repo too.

## Policy

`host.data` MUST contain ONLY:

| Field | Why allowed |
|---|---|
| `ssh_user` | required by pyinfra ssh connector |
| `ssh_port` | required by pyinfra ssh connector |
| `ssh_hostname` | required by pyinfra ssh connector |
| `tenant_name` | small identifier, used by every task for templating + logging |
| `tenant_type` | small enum, used to gate `internal`-only operations |

`host.data` MUST NOT contain:

- Full tenant config dict (no `tenant_config` key)
- Contact info
- Secret refs
- Notes
- Any field not listed above

## Access pattern for tasks that need full config

Task modules that need specific config values (e.g. agent-install task needs `agent.llm.model`) call a helper:

```python
# pyinfra/lib/host_helpers.py
from pyinfra import host
from lib.tenant_loader import TenantConfig, load_tenant
from pathlib import Path
import os

def get_tenant_config(host=host) -> TenantConfig:
    """Load the current host's tenant.yaml on-demand.

    Reads tenant_name from host.data and resolves data_repo from env.
    Returns a fully-validated TenantConfig.

    Loaded fresh each call — no caching across tasks. Cheap (single YAML parse).
    """
    tenant_name = host.data.tenant_name
    data_repo = Path(os.environ.get(
        "BUBBLE_DATA_REPO",
        Path(__file__).resolve().parent.parent.parent / "bubble-vps-data",
    )).expanduser().resolve()
    return load_tenant(tenant_name, data_repo)
```

The full config is reloaded inside the task's process, NOT serialized into pyinfra's host data, NOT shipped to the remote host, NOT logged unless the task explicitly does so.

This means:
- `pyinfra --debug` only shows the small `host.data` dict — no PII leak
- `--dry-run` only sees the small dict
- Multi-tenant deploys (`TENANTS_ALL=1`) keep each tenant's full config in its own process scope

## Helper module location

`bubble-vps-platform/lib/host_helpers.py` — new file. Sits next to `tenant_loader.py`.

## Test plan

1. `test_host_data_only_contains_allowed_keys()` — call `_build_host_entry(cfg)` and assert the returned dict has EXACTLY the 5 allowed keys plus `persona_dir` and `secrets_file` (operator-Mac paths, also not host-side metadata risk but DO contain tenant name in the path — acceptable since `tenant_name` is already in host.data).

   Wait — re-reading: `persona_dir` and `secrets_file` are operator-Mac-side absolute paths. They include the data repo path which is operator-side, not host-side. They're useful to tasks (e.g. rsync source). They reveal where on the operator's Mac the data repo lives, which is mildly identifying but already known to anyone with shell on the operator. Keeping them.

   Final allowed keys: `ssh_user, ssh_port, ssh_hostname, tenant_name, tenant_type, persona_dir, secrets_file`. NOT `tenant_config`, NOT contact, NOT notes.

2. `test_get_tenant_config_helper()` — set up a fake host.data with `tenant_name=bubble-internal`, call `get_tenant_config()`, assert it returns a valid TenantConfig with full fields.

3. `test_host_data_dump_no_pii()` — dump host_data via repr() / json.dumps and grep for "{{OPERATOR_EMAIL}}", "{{OPERATOR_CHAT_ID}}", "{{HETZNER_SERVER_ID}}". Must not appear.

4. Re-run all Step 1 tests + the hello-world deploy. Must still pass byte-identically.

## Migration

`deploy.py` (Step 1's hello-world) doesn't currently use `tenant_config` — it only reads `host.data.tenant_name`. So this change is non-breaking for Step 1.

Step 2's `tasks/hardening/linux.py` will be the first consumer of `get_tenant_config(host)`.

## Out of scope

- Encrypting the data repo at rest (separate concern, age-encrypted secrets handle the secret bits at Step 3)
- Filtering pyinfra's own logs (we don't control pyinfra's logging — best we can do is not feed it sensitive data in the first place)
