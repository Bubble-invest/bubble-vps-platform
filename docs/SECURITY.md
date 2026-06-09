# SECURITY — threat model + procedures

What we defend against, how, and the runbooks for when something goes wrong. Architecture context: [ARCHITECTURE.md](ARCHITECTURE.md). Detailed contracts: SPEC-* docs under [`../specs/`](../specs/).

---

## Threat model

Adapted from [SPEC-006](../specs/SPEC-006-secrets-sops-age.md) §"Threat model" with the operator-Mac and GitHub-repo dimensions added:

| Adversary | Capability | Mitigation |
|---|---|---|
| Attacker reads the data repo (e.g. accidental public push) | Reads encrypted blob | Cannot decrypt without an age private key — SOPS+age recipient list is the gate |
| Attacker on the box with shell as `claude` | Reads `/run/claude-agent/env` (root-owned, 0400) | Cannot read — wrong UID |
| Attacker with root on the box | Can read `/run/claude-agent/env` while service runs | Has full system control anyway; secrets are momentarily exposed but blast radius is limited to that one tenant. Rotation propagates new values via `operator-set-secret.sh` + redeploy |
| Hetzner provider taking a snapshot | Reads disk image | tmpfs `/run/` is RAM-only; plaintext NEVER touches the disk |
| **Operator-Mac compromise** | Reads master age private key | **Catastrophic** — Mac is the root of trust; if it falls, ALL tenants compromised. Mitigated by macOS Keychain protection + FileVault (hardware-backed when enabled). Rotation procedure under §"Key rotation" below |
| **Tenant-box compromise** | Reads `/etc/age/key.txt` (root, 0400) + filesystem | Single-tenant blast radius. Rotation = regenerate box keypair, update `.sops.yaml`, `sops updatekeys`, redeploy |
| **GitHub repo compromise** (data repo gets stolen) | Same as accidental public push | Same — encrypted blob unreadable without an age private key. The data repo carries no plaintext secrets |
| Process inspection (`/proc/<pid>/environ`) | Anyone with same UID or root reads env vars | Same UID = the agent's own UID, fine. Root = catastrophic anyway |
| Transcript bleed (Claude reads .env into a JSONL) | Same risk we have on the Mac side | Mitigated: agent never has direct read access to `/run/claude-agent/env` (systemd injects vars, agent inherits them; the file is root-only) |
| Stolen Tailscale auth key | Attacker can join the tailnet as a tenant | Auth keys are per-tenant + reusable; revoke via Tailscale admin → device list. ACL scoping by `tag:bubble-tenant` limits what a rogue device can reach. See [SPEC-011](../specs/SPEC-011-tailscale.md) |
| Stolen Hetzner API token | Attacker can create/destroy boxes in our project | Token lives in operator-Mac Keychain only, never in transcripts. Rotation via Hetzner console → revoke + reissue |

**Plaintext-on-disk attack surface is reduced to ZERO on tenant boxes.** The only persistent secret material on the box is the age private key in `/etc/age/key.txt` (root:root 0400). Compromise of that key + filesystem read access = decrypt that one tenant's secrets.

---

## Hardening profile summary

Configured by `pyinfra/tasks/hardening/` per [SPEC-005](../specs/SPEC-005-linux-hardening.md). Verified by the drift-test in [RUNBOOK.md](RUNBOOK.md) §"How to verify the hardening playbook is healthy".

- **sshd:** PermitRootLogin no, PasswordAuthentication no, MaxAuthTries 3, KbdInteractiveAuthentication no, ChallengeResponseAuthentication no, LoginGraceTime tightened. Drop-in at `/etc/ssh/sshd_config.d/00-bubble-hardening.conf` (load order before cloud-init's 50-).
- **UFW:** default deny incoming, allow outgoing, `limit 22/tcp` (6 conns / 30s / IP — see [SPEC-004](../specs/SPEC-004-ssh-rate-limit-policy.md)).
- **fail2ban:** sshd jail in aggressive mode + recidive jail. Config at `/etc/fail2ban/jail.local`.
- **unattended-upgrades:** security-only origins, auto-reboot 04:00 UTC, configured at `/etc/apt/apt.conf.d/52unattended-upgrades-bubble`.
- **Swap:** 2 GB swapfile, `vm.swappiness=10` in `/etc/sysctl.d/99-bubble-swap.conf`.
- **chrony:** installed + enabled (avoids time-skew breaking TLS / fail2ban windows).

What we deliberately do NOT manage in v1: SSH key deployment (assumed present at provisioning), non-root user creation (same), disk encryption / LUKS (Hetzner image baseline), AppArmor / SELinux (Ubuntu 24.04 defaults), auditd (overkill for our scale; add if a client requires SOC2/ISO27001).

---

## Secrets management

Mechanism: SOPS + age, two recipients per tenant (operator master + tenant box). Detailed contract: [SPEC-006](../specs/SPEC-006-secrets-sops-age.md), [SPEC-008](../specs/SPEC-008-secrets-deploy-second-half.md).

### Hard rules

These are non-negotiable. Violations have caused real incidents and are codified in the playbook code:

1. **No `sops --decrypt` output ever reaches stdout, stderr, or any agent transcript.** Per [SPEC-008](../specs/SPEC-008-secrets-deploy-second-half.md) §"Hard rule (the lesson from 2026-05-08)". Allowed patterns: exit-code check, length-only signature, per-key `grep -q` existence check. Forbidden: any pattern that prints plaintext values, even briefly, even to a temp file outside tmpfs.

2. **Plaintext only ever lives in tmpfs at `/run/claude-agent/env`** (mode 0400, root:root, mounted in RAM). Decryption happens in systemd ExecStartPre at service start; the file is wiped when tmpfs is unmounted (reboot or service stop).

3. **The `TAILSCALE_AUTHKEY` in `tailscale up` uses the file:auth-key form** rather than passing the value on the command-line, per [SPEC-011](../specs/SPEC-011-tailscale.md). Rationale: command-line args appear in `/proc/<pid>/cmdline` and historic shell history; file:auth-key reads from a file-descriptor without ever materializing in argv.

4. **Operator scripts that ingest secret values use a native GUI password prompt** (`osascript display dialog -with hidden answer` on macOS; `gum input --password` on Linux). No terminal echo, no scrollback contamination. See `scripts/operator-set-secret.sh` for the implementation.

5. **The Hetzner API token, GitHub token, and Tailscale OAuth credentials live in macOS Keychain only.** Read via `security find-generic-password -w` into a one-shot env var, never persisted, never echoed. See `scripts/provision-tenant.sh` and `scripts/offboard-tenant.sh`.

### Recipient model

`bubble-vps-data/.sops.yaml` lists per-file recipient blocks. Each tenant's `secrets.sops.env` has TWO recipients:

- The operator master pubkey (one entry, shared across all tenants you manage)
- That tenant's box pubkey (unique per tenant, generated on first deploy)

Adding/removing a recipient = edit `.sops.yaml`, run `sops updatekeys --yes <file>`. The encrypted blob is re-encrypted with the new recipient list; the old recipients can no longer decrypt.

---

## Key rotation

### Operator master age key (catastrophic event — full re-keying)

If the operator Mac is suspected compromised:

1. From a **clean machine** (NOT the suspected one): generate a new master key with `age-keygen`. Capture the new pubkey.
2. From the suspected machine, **immediately** revoke access at the network level — disable Tailscale tagOwner, revoke Hetzner API token, revoke GitHub PAT.
3. For each tenant, edit `bubble-vps-data/.sops.yaml` to swap the old operator master pubkey for the new one in every recipient block, then `sops updatekeys --yes tenants/<name>/secrets.sops.env`. The old master key can no longer decrypt anything.
4. Rotate every secret value in every tenant — they were potentially exposed. Use `operator-set-secret.sh` per key per tenant.
5. Redeploy every tenant.

This is the **single point of failure** in the design. Hardening: FileVault, Keychain, no plaintext age keys in cloud sync (iCloud, Dropbox), no email backups.

### Per-tenant box age key

If a tenant box is suspected compromised but not destroyed:

1. SSH to the box, regenerate the keypair: `sudo age-keygen -o /etc/age/key.txt.new && sudo mv /etc/age/key.txt.new /etc/age/key.txt && sudo chmod 400 /etc/age/key.txt && age-keygen -y /etc/age/key.txt | sudo tee /etc/age/key.pub`.
2. Capture the new pubkey, replace the old one in `bubble-vps-data/.sops.yaml` for that tenant.
3. `sops updatekeys --yes tenants/<name>/secrets.sops.env`.
4. Rotate every secret value (assume they were read off the box). Use `operator-set-secret.sh` per key.
5. Redeploy: `./scripts/deploy.sh --tenant=<name>`. The agent service restart picks up the new secrets.

### Per-secret rotation (routine — e.g. quarterly OpenRouter key rotation)

See [RUNBOOK.md](RUNBOOK.md) §"Secret needs rotation". Single-tenant: `operator-set-secret.sh` + `deploy.sh`. Multi-tenant: loop manually for now (batch script not in Phase 1).

---

## Incident response — suspected tenant compromise

Run, in order:

1. **Disconnect Tailscale** for the suspect box at the admin level: [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines) → find the device → "Disable" (NOT "Remove" — disable preserves the entry for forensics).
2. **Snapshot the Hetzner box** for forensics: `hcloud server create-image <server-id> --type snapshot --description "incident-<date>"`. Cost is negligible; deletion is reversible from the snapshot for ~7 days.
3. **Audit transcripts.** SSH to the box (now Tailscale-only via re-enable temporarily, or via Hetzner console). Review `/home/claude/.claude/projects/*/*.jsonl` for the suspected-leak window. The security-audit cron's Part 6 ([SPEC-014](../specs/SPEC-014-cloud-security-cron.md)) has the credential-prefix scan logic — adapt the regex for the suspected leaked value.
4. **Rotate every secret** for that tenant per §"Per-tenant box age key" above.
5. **Decision point: rebuild or continue.** If the box is suspected rooted (not just a credential leak), destroy it: `./scripts/offboard-tenant.sh <name> --mode=destroy` then `./scripts/new-tenant.sh + provision-tenant.sh + deploy.sh` from a known-clean state. If just a leaked secret, rotation is sufficient.
6. **Notify the client** per the DPA's incident-response clause (timeline + scope of exposure).

For the offboarding flows (handoff vs destroy), see [OFFBOARDING.md](OFFBOARDING.md).

---

## Sudoers grants per cron

Each tenant box has narrow `NOPASSWD` sudoers grants for the automation crons. They are scoped to specific commands, NOT broad shell access. Audited daily by the security-audit cron's Part 1 (any drift from the expected baseline is flagged).

| Cron | Grant file | Allowed commands |
|---|---|---|
| `bubble-security-audit.service` | `/etc/sudoers.d/claude-security-audit` | `fail2ban-client status sshd`, `sshd -T`, `last -F -50`, `cat /etc/sudoers.d/*`, `cat /etc/passwd`, file-mode reads under `/etc/age/`, `/etc/bubble/`, `/run/claude-agent/`. Read-only. See [SPEC-014](../specs/SPEC-014-cloud-security-cron.md). |
| `telegram-watchdog.service` | `/etc/sudoers.d/claude-telegram-watchdog` | `systemctl restart claude-agent-<persona>.service` only — nothing else. See [SPEC-013](../specs/SPEC-013-telegram-recovery-watchdog.md). |
| `bubble-phone-home.service` | (no sudo) | Runs as `claude`; reads only telemetry that's accessible without sudo. |

Grants are deployed by `pyinfra/tasks/monitoring/` and `pyinfra/tasks/access/`. The expected baseline is encoded in the security-audit cron itself — any unexpected NOPASSWD grant triggers an alert.

---

## What's deliberately out of scope for v1

- **Hardware security module (HSM) for the master age key** — overkill at our tenant count; revisit when we have >10 paying clients
- **Auditd** — log-volume cost vs marginal value; revisit if a client requires SOC2/ISO27001
- **Per-secret access logging on the box** — the secrets layer is "all-or-nothing" (the systemd unit reads everything in `/run/claude-agent/env` at start); selective audit needs a different design (vault-style)
- **Network egress filtering** — the agent legitimately reaches many hosts (Anthropic, Telegram, npm, GitHub, MCP servers); allowlist is impractical
- **Multi-recipient SOPS for shared secrets** — every secret is per-tenant in v1; if we ever need a "shared between tenants A, B, C" secret, the recipient model already supports it but no helper script exists yet

These are revisited as the platform scales beyond Phase 1.
