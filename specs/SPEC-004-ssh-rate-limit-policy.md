# SPEC-004 — SSH rate-limit policy

**Status:** Draft v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-08
**Reviewed by:** _pending Joris approval_
**Addresses:** Step 1 Finding #1 (intermittent SSH connection failures during rapid reconnects)

---

## Investigation

UFW on joris-cx33 has `LIMIT IN` on port 22:

```
[ 1] 22/tcp                     LIMIT IN    Anywhere    # SSH (rate-limited: 6 conns/30s/IP)
```

This translates to a Linux kernel `recent` module rule that **drops connections from a source IP after 6 connection attempts in any 30-second window**. fail2ban is also active but with a separate (more permissive) threshold.

Tested locally: `for i in 1..5; do nc -z box 22; done` — first 3 connections succeed, 4th and 5th time out. Confirms the limit fires at the 4th rapid connection (the `recent` module counts SYNs in a sliding 30s window).

This is **intentional hardening behavior**, working as designed. It's not a bug — it's defense against SSH brute-force.

## Real-world impact on pyinfra

A normal pyinfra deploy opens **1 SSH connection per host**, which it reuses for the whole run (paramiko ControlMaster-style). It does NOT create a new connection per operation. So in normal operation, the rate limit is a non-event.

Failure modes that DO trigger it:
1. **Operator reruns a deploy 6+ times in 30 seconds** (e.g. iterating on a broken task — possible during dev)
2. **Multi-tenant deploys to many hosts at once** — `TENANTS_ALL=1` against 6+ Hetzner boxes — but this is per-source-IP, so on the *box* side each tenant box only sees 1 connection from the operator, fine
3. **A flaky network causing reconnects** — 6+ retries within 30s could trip it
4. **Concurrent operators** running deploys from different machines — each is its own source IP, fine

The ONLY scenario where this bites us is dev iteration on a single host (case 1).

## Policy

**Do not loosen the firewall rule.** 6 conn / 30s is a security-meaningful limit — relaxing it weakens defense against rapid-fire credential-spray attacks.

**Do add resilience on the client side** so transient drops are handled gracefully:

1. `scripts/deploy.sh` wrapper passes `--retry 2 --retry-delay 5` to pyinfra by default. pyinfra retries failed **operations** with delay between attempts. (Note: pyinfra 3.8 has no SSH-connection-level retry flag; connection failures are not retried at all by pyinfra itself, so this provides operation-level resilience instead.)
2. Document the limit in `docs/INSTALL.md` so operators understand WHY a 7th rapid retry will be slow.
3. **Future** (Step 6): Tailscale connection terminates locally on the box, bypassing the public SSH path. Once Tailscale is the primary control plane, the public-IP rate limit only matters as a fallback.

## Implementation

In `scripts/deploy.sh`:

```bash
DEFAULT_FLAGS=("--retry" "2" "--retry-delay" "5")
exec pyinfra "${DEFAULT_FLAGS[@]}" inventory.py deploy.py "${EXTRA_ARGS[@]}"
```

Operator can override by re-passing their own `--retry`/`--retry-delay`. pyinfra uses the last-passed values.

**Honest scope note:** if SSH connection itself fails (rate-limit cuts the SYN), pyinfra exits before retrying. The `--retry` flag retries failed operations *after* a connection is established. For connection-level retries, the operator wraps the wrapper:

```bash
# Outer retry harness for tight dev iteration:
for i in 1 2 3; do
    ./scripts/deploy.sh --tenant=bubble-internal && break
    echo "deploy attempt $i failed, sleeping 30s..."
    sleep 30
done
```

This is documented in INSTALL.md as the recommended pattern when iterating fast on a single box.

In `docs/INSTALL.md`, add a "Troubleshooting" section explaining:

> If a deploy fails with `Connection refused` or `Connection timed out` during a rapid re-run, you've likely tripped the box's SSH rate limit (6 connections / 30 seconds, configured in UFW for security). Wait 30 seconds and retry. The wrapper script automatically retries 3 times with backoff, which handles most transient cases.

## Test plan

1. `test_deploy_sh_passes_retries_flag()` — invoke wrapper with `--dry-run` and grep its rendered command for `--ssh-retries=3`.
2. (Manual) Run `scripts/deploy.sh --tenant=bubble-internal` 7 times in 60 seconds; verify final invocation eventually completes (because the wrapper retries with backoff that allows the rate window to clear).

## Out of scope

- Per-IP allowlist for operator Mac (we move around — wifi at home, café, office). Not maintainable.
- Replacing UFW with a different firewall — works fine.
- Hetzner Cloud Firewall layer — already permissive (allow 22/tcp from anywhere), the bottleneck is the host-side UFW rule.

## Cross-ref

- Step 6 will install Tailscale, replacing public-IP SSH as the primary path; this rate-limit only governs the fallback path after that point.
