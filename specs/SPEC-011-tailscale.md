# SPEC-011 — Tailscale install + tenant join (Step 6a)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Steps 1-5a done; SOPS+age secrets layer working
**Implements:** Step 6a of the Bubble VPS Platform build plan

---

## Purpose

Install the Tailscale agent on every tenant box and register it with our tailnet using a SOPS-encrypted auth key. Once active:

1. Operator (Joris) can SSH any tenant box via `ssh <hostname>` (Tailscale MagicDNS resolves the name) — bypasses public-IP rate-limits, works from any network
2. Future per-tenant ACLs can scope which operator devices reach which tenant (per `access.tailscale.tags` in tenant.yaml)
3. Phone-home daemon (Step 6b) and central dashboard (Step 6c) can talk to each tenant over the mesh
4. Tenant box does NOT advertise routes or accept routes (avoid network coupling)

---

## Pre-requisites (from operator side, one-time)

1. **Tailscale account exists** — `jorisdupraz@gmail.com` tailnet `tail408dcc.ts.net` confirmed
2. **`tag:bubble-tenant` defined in tailnet ACL** — operator added `"tag:bubble-tenant": ["jorisdupraz@gmail.com"]` to tagOwners
3. **Reusable + pre-approved + tagged auth key generated** at admin/settings/keys — pasted via `operator-set-secret.sh --key=TAILSCALE_AUTHKEY`
4. **`TAILSCALE_AUTHKEY` in tenant's secrets.sops.env** — added to `required_keys` in tenant.yaml

---

## tenant.yaml — already has the access block

The existing block (added in Step 1, currently has `enabled: true` but no installer wired):

```yaml
access:
  tailscale:
    enabled: true
    authkey_secret_ref: TAILSCALE_AUTHKEY
    tags:
      - "tag:tenant"
      - "tag:tenant-bubble-internal"
    accept_routes: false
    advertise_routes: []
```

**Updates per SPEC-011:**
- `tags` should be `["tag:bubble-tenant"]` (single, simpler — per-tenant scoping comes via hostname-based ACLs later, not per-tag)
- Add `hostname: <tenant_name>` (Tailscale uses this as the device name; defaults to the box's system hostname which is also fine)

---

## What the pyinfra task does

`pyinfra/tasks/access/tailscale.py`:

```python
def apply():
    cfg = get_tenant_config(host)
    ts = cfg.access.tailscale
    if not ts.enabled:
        return  # opt-out for tenants that don't want our managed mesh

    # 1. Install Tailscale via official Ubuntu repo
    #    https://tailscale.com/download/linux/ubuntu-24.04
    apt.repo(...)  # add pkgs.tailscale.com Ubuntu noble stable
    apt.packages(name='tailscale', present=True, update=False)

    # 2. Ensure tailscaled service is enabled + running
    systemd.service(name='tailscaled', running=True, enabled=True)

    # 3. Run `tailscale up` ONLY if not already authenticated
    #    Idempotency check via `tailscale status --json | jq .Self.ID`
    #    If we already have a NodeID, skip the `tailscale up` call.
    server.shell(
        name="access/tailscale: register with tailnet (skip if already registered)",
        commands=[
            "if ! tailscale status --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get(\"Self\",{}).get(\"Online\") else 1)' 2>/dev/null; then "
            "  tailscale up "
            "    --auth-key=\"${TAILSCALE_AUTHKEY}\" "
            "    --advertise-tags=tag:bubble-tenant "
            "    --hostname=" + persona_hostname + " "
            "    --accept-routes=false "
            "    --advertise-routes= "
            "    --reset; "
            "fi"
        ],
        # Read auth key from /run/claude-agent/env (or a separate env file).
        # See "Auth-key access pattern" below.
    )

    # 4. Verify connectivity:
    #    - `tailscale status` shows Online
    #    - `tailscale ip` returns a 100.x.x.x address
    server.shell(
        name="access/tailscale: verify online",
        commands=["tailscale status --json | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d[\"Self\"][\"Online\"], \"not online\"; print(\"tailscale ip:\", d[\"Self\"][\"TailscaleIPs\"])'"],
    )
```

---

## Auth-key access pattern (the careful bit)

`TAILSCALE_AUTHKEY` is in `/run/claude-agent/env` (the agent's env file). But the Tailscale registration happens at deploy time as ROOT, not as the claude user. Two options:

**Option A — read from the agent env file:**
```python
server.shell(
    commands=[". /run/claude-agent/env && tailscale up --auth-key=$TAILSCALE_AUTHKEY ..."],
    _sudo=True,
)
```
Works because `/run/claude-agent/env` is mode 0400 root:root + claude:claude (root can read). Dependency: agent service must have started at least once to create the file.

**Option B — decrypt fresh from /etc/bubble/secrets.sops.env:**
```python
server.shell(
    commands=[
        "AUTHKEY=$(SOPS_AGE_KEY_FILE=/etc/age/key.txt /usr/local/bin/sops --decrypt /etc/bubble/secrets.sops.env 2>/dev/null | grep '^TAILSCALE_AUTHKEY=' | cut -d= -f2-) && "
        "tailscale up --auth-key=\"$AUTHKEY\" --advertise-tags=tag:bubble-tenant --hostname=joris-cx33 --accept-routes=false --advertise-routes= --reset"
    ],
    _sudo=True,
)
```
Works without depending on the agent service. Self-contained.

**Recommendation: Option B.** Tailscale install can land before agent service is fully working (e.g. on first deploy of a new tenant). And it follows the SPEC-008 hard rule: stderr redirected to `/dev/null`, plaintext value captured into `$AUTHKEY` shell variable, used immediately, never echoed.

---

## SPEC-008 hard rule compliance

`tailscale up --auth-key=$AUTHKEY` would show the auth key in `ps auxww` for the duration of the call. **Confirmed via `tailscale up --help`:** the `file:` prefix is supported — `--auth-key=file:/path/to/file`.

**Use the file: form.** Write the auth key to a tmpfs file (mode 0400 root:root), pass `--auth-key=file:/run/tailscale-authkey`, then immediately `rm` the file. Plaintext value never appears in process listing, never in ps output, never in deploy logs.

```bash
# Decrypt + write to tmpfs file (NOT echo'd)
SOPS_AGE_KEY_FILE=/etc/age/key.txt /usr/local/bin/sops --decrypt /etc/bubble/secrets.sops.env 2>/dev/null \
    | grep '^TAILSCALE_AUTHKEY=' | cut -d= -f2- > /run/tailscale-authkey
chmod 0400 /run/tailscale-authkey

# Use it (no value in ps output)
tailscale up --auth-key=file:/run/tailscale-authkey \
    --advertise-tags=tag:bubble-tenant \
    --hostname=joris-cx33 \
    --accept-routes=false --advertise-routes= --reset

# Wipe immediately
rm -f /run/tailscale-authkey
```

Done in a single `server.shell` `commands=[...]` block so failure mid-way still triggers the cleanup (or use a `trap` if pyinfra splits commands).

---

## Idempotency

Re-running the task should report 0 changes if:
- Tailscale package already installed
- Service already enabled+running
- Box already registered (`tailscale status` shows Online with our tailnet)

Specifically: the `tailscale up` call MUST NOT re-run on every deploy. Two ways to guard:
1. Pre-check fact: `tailscale status --json` returns Online → skip
2. Use `_if=` pyinfra clause based on a fact

---

## Acceptance criteria

Step 6a is DONE when:
1. ✅ Tailscale apt package installed on box
2. ✅ `tailscaled` service active + enabled
3. ✅ Box registered with tailnet, visible in https://login.tailscale.com/admin/machines
4. ✅ `ssh joris-cx33` from operator's Mac succeeds via Tailscale (resolves to 100.x.x.x)
5. ✅ Box's hostname appears with `tag:bubble-tenant` in admin console
6. ✅ Re-running pyinfra task is idempotent (zero changes on second deploy)
7. ✅ Public-IP SSH (`ssh -4 178.105.77.178`) still works as fallback (UFW LIMIT IN remains in place)
8. ✅ Tests pass; deploy logs grep clean for `tskey-` and any other secret prefix

---

## Out of scope (deferred)

- ACL refinement (per-tenant scoping via `tag:tenant-bubble-internal` etc) — Step 6b
- Phone-home daemon talking to dashboard via Tailscale — Step 6c
- Telegram-plugin recovery patterns (your reminder) — separate, will queue after Step 6
- Tailnet-only services (dashboard exposing only on Tailscale IP) — Step 6c

---

## Open questions

1. **`--reset` flag risky?** It clears any prior `tailscale up` settings. Probably fine for our greenfield setup; document in case operator runs into existing state.
2. **What if the auth key has expired?** `tailscale up` will fail with a clear error. The task should report this gracefully (not silently break).
3. **MagicDNS hostname collision?** If two tenants have the same hostname, Tailscale auto-numbers them. We should use the tenant_name as the hostname (e.g. `joris-cx33` for our box, `acme-corp-vps` for client #1).
