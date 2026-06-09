"""Ship the encrypted secrets blob to the box and verify on-box decryption
(SPEC-008 — Step 3 Phase D second half).

What this does, idempotently:
    1. Ensure /etc/bubble/ exists (mode 0755, root:root).
    2. Upload bubble-vps-data/tenants/<name>/secrets.sops.env to
       /etc/bubble/secrets.sops.env (mode 0440, root:root). pyinfra hashes
       both sides; only re-uploads if content differs.
    3. Test-decrypt on the box via `sops --decrypt` — EXIT CODE ONLY. The
       command's stdout is redirected to /dev/null; non-zero exit fails the
       deploy with a clear pyinfra error. No plaintext value reaches pyinfra
       stdout/stderr (and therefore never reaches operator transcripts).
    4. For each `required_keys` entry from cfg.secrets.required_keys, verify
       the key is present in the decrypted output via `grep -q` — a quiet
       existence check that exits non-zero (failing the deploy) if the key
       is missing. Again: no value ever printed.

Hard rule (the lesson from 2026-05-08):
    No `sops --decrypt` output ever reaches stdout/stderr. ALL invocations
    in this module are followed by either `> /dev/null` (exit code only) or
    `| grep -q '...'` (existence check, no value visible). This is enforced
    statically by `lib/test_secrets_layer.py` so future edits can't slip a
    plaintext leak past review.

Idempotency:
    - files.directory: pyinfra default
    - files.put: content-aware (hashes both sides)
    - server.shell verifications: always re-run, but no state mutation —
      pyinfra reports "Success" each run with no diff.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.operations import files, server

from lib.host_helpers import get_tenant_config


_ETC_BUBBLE = "/etc/bubble"
_DEFAULT_ENCRYPTED_PATH = "/etc/bubble/secrets.sops.env"
_DEFAULT_AGE_KEY_PATH = "/etc/age/key.txt"
_SOPS_BIN = "/usr/local/bin/sops"


def _local_encrypted_src(host_) -> Path:
    """Operator-Mac-side path to this host's secrets.sops.env.

    Resolved by inventory.py as `<data-repo>/tenants/<tenant>/secrets.sops.env`
    and stashed into `host.data.secrets_file`. Reading it through host.data
    keeps this module agnostic of how the data repo is laid out — it's the
    same lookup hardening tasks use.
    """
    return Path(host_.data.secrets_file)


def apply() -> None:
    """Deploy + verify the encrypted secrets blob on the box.

    Caller (deploy.py orchestrator) only invokes this when cfg.secrets is
    enabled, but we double-check defensively — the module should be safe to
    import and apply directly without the orchestrator.
    """
    cfg = get_tenant_config(host)
    s = cfg.secrets
    if s is None or not s.enabled:
        # No-op: secrets layer disabled for this tenant. Caller should have
        # filtered, but belt-and-braces.
        return

    encrypted_path = s.encrypted_file_path or _DEFAULT_ENCRYPTED_PATH
    age_key_path = s.age_key_path or _DEFAULT_AGE_KEY_PATH

    # 1) /etc/bubble/ — owner root:root, mode 0755 (the dir itself isn't
    #    sensitive; its contents are). Mirrors _age_setup's /etc/age treatment.
    files.directory(
        name="secrets/sops_deploy: ensure /etc/bubble exists",
        path=_ETC_BUBBLE,
        user="root",
        group="root",
        mode="0755",
        present=True,
    )

    # 2) Upload the encrypted blob from the data repo to the box. pyinfra's
    #    files.put is content-aware: it hashes both sides and only transfers
    #    when bytes differ. Mode 0440 root:root (see SPEC-008 §"File modes").
    #    Captured into upload_op so the restart-on-change op below can gate
    #    on `upload_op.did_change` (SPEC-012).
    local_src = _local_encrypted_src(host)
    upload_op = files.put(
        name=f"secrets/sops_deploy: upload encrypted secrets to {encrypted_path}",
        src=str(local_src),
        dest=encrypted_path,
        user="root",
        group="root",
        mode="0440",
    )

    # 3) Test-decrypt on the box — EXIT CODE ONLY. `> /dev/null` discards the
    #    decrypted plaintext; the shell command exits 0 if and only if sops
    #    succeeded. If the box's age key isn't a recipient (or the file is
    #    corrupt), this fails loud — pyinfra reports the error and aborts.
    #
    #    SOPS_AGE_KEY_FILE tells sops where to find the box's age private key.
    #    We pass it inline (not via env-file) so this command is fully
    #    self-contained and doesn't depend on /etc/environment etc.
    server.shell(
        name="secrets/sops_deploy: verify on-box decryption (exit code only)",
        commands=[
            f"SOPS_AGE_KEY_FILE={age_key_path} {_SOPS_BIN} --decrypt "
            f"{encrypted_path} > /dev/null"
        ],
    )

    # 4) Per-key existence check — ONE grep -q per required key. `grep -q`
    #    exits 0 on first match, 1 on no match; it produces no stdout. The
    #    decrypted plaintext is piped through grep but never written
    #    anywhere visible — it lives only in the pipe between sops and grep
    #    and is discarded the moment grep finds (or doesn't find) the key.
    #
    #    Anchored at start of line (^KEY=) so we match dotenv lines exactly,
    #    not substrings of other values.
    for key in s.required_keys:
        server.shell(
            name=(
                f"secrets/sops_deploy: verify required key {key} present "
                f"(existence only)"
            ),
            commands=[
                f"SOPS_AGE_KEY_FILE={age_key_path} {_SOPS_BIN} --decrypt "
                f"{encrypted_path} | grep -q '^{key}='"
            ],
        )

    # 5) Restart the agent service IF the encrypted file changed (SPEC-012).
    #    The systemd unit's ExecStartPre re-decrypts /etc/bubble/secrets.sops.env
    #    into /run/claude-agent/env at every (re)start. Without this trigger,
    #    a refreshed encrypted blob lives on disk but the running agent keeps
    #    its OLD decrypted env until the operator manually restarts. Bit us
    #    during Step 6a when TAILSCALE_AUTHKEY was added.
    #
    #    Mirrors the pattern in agent/_settings.py: capture the upload op,
    #    gate the restart on `upload_op.did_change`, sudo for systemctl.
    #
    #    Edge case (cold deploy): on the first-ever run the agent service
    #    doesn't exist yet (_systemd_unit hasn't run). The
    #    `systemctl list-unit-files <name> >/dev/null 2>&1` guard makes the
    #    command a no-op in that case — echoes a friendly message and exits 0.
    #
    #    Persona name comes from cfg.agent.concierges — never hardcoded so the
    #    module stays tenant-portable (per SPEC-003 + SPEC-012 § "Implementation"
    #    item 3).
    #
    #    MULTI-CONCIERGE (SPEC-001 v1.2): the shared encrypted blob feeds EVERY
    #    concierge's service (each re-decrypts it into its own runtime env dir at
    #    (re)start), so a refreshed blob must restart ALL of them — loop over
    #    cfg.agent.concierges, one gated restart per service.
    for concierge in cfg.agent.concierges:
        service_name = f"claude-agent-{concierge.name}.service"
        server.shell(
            name=f"secrets/sops_deploy: restart {service_name} if secrets file changed",
            commands=[
                f"systemctl list-unit-files {service_name} >/dev/null 2>&1 && "
                f"systemctl restart {service_name} || "
                f"echo 'service not yet installed — first-deploy path; _systemd_unit will start it'"
            ],
            _if=upload_op.did_change,
            _sudo=True,
        )
