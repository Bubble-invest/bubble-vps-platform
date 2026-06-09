"""Generate the per-tenant box age keypair (SPEC-006 §"Generation").

What this does, idempotently:
    1. Ensures /etc/age exists (mode 0755, root:root).
    2. If /etc/age/key.txt is missing, runs `age-keygen` to create it
       (mode 0400, root:root). Existing keys are NEVER overwritten.
    3. If /etc/age/key.pub is missing, derives it from the private key.
    4. Copies the pubkey BACK to the operator's data repo at
       `tenants/<tenant>/box-pubkey.txt` so Joris can append it to
       `.sops.yaml` and run `sops updatekeys` (the manual gate before
       Phase D's second half can run).

Idempotency:
    The shell guards (`test -f ... ||`) ensure subsequent runs do nothing
    on the box. files.get re-downloads every run but is content-stable —
    the local file is overwritten with the same bytes when the box pubkey
    hasn't changed (file content doesn't drift between runs once generated).
"""

from __future__ import annotations

from pathlib import Path

from pyinfra import host
from pyinfra.facts.files import File
from pyinfra.operations import files, server

from lib.host_helpers import get_tenant_config


_AGE_DIR = "/etc/age"
_AGE_KEY = "/etc/age/key.txt"
_AGE_PUB = "/etc/age/key.pub"


def _local_pubkey_dest(host_) -> Path:
    """Where the box pubkey lands on the operator Mac.

    Per SPEC-006 §"Generation", the file lives next to tenant.yaml:
        bubble-vps-data/tenants/<tenant>/box-pubkey.txt

    `host_.data.persona_dir` is the Mac-side absolute path to
    `<data-repo>/tenants/<tenant>/persona/<persona-name>` (resolved by
    inventory.py). The tenant directory is its grandparent.
    """
    persona_dir = Path(host_.data.persona_dir)
    tenant_dir = persona_dir.parent.parent  # tenants/<tenant>/
    return tenant_dir / "box-pubkey.txt"


def apply() -> None:
    """Generate the box age keypair if missing, copy pubkey back to operator."""
    # 1) /etc/age — owner root:root, mode 0755 (the dir itself isn't sensitive;
    #    its contents are).
    files.directory(
        name="secrets/age: ensure /etc/age exists",
        path=_AGE_DIR,
        mode="0755",
        user="root",
        group="root",
        present=True,
    )

    # 2) Generate the private key if missing. `umask 077` belt-and-braces in
    #    case age-keygen doesn't already chmod 0400 (it does, but we don't rely
    #    on that — every server.shell command runs in a fresh shell so the
    #    umask only affects this command's child processes). Then `chmod 0400`
    #    explicitly. NEVER overwrites an existing key.
    keyfile_present = host.get_fact(File, path=_AGE_KEY) is not None
    if not keyfile_present:
        server.shell(
            name="secrets/age: generate per-tenant box keypair",
            commands=[
                # All on one shell line so `umask` applies to age-keygen.
                "umask 077 && /usr/local/bin/age-keygen -o /etc/age/key.txt && "
                "chmod 0400 /etc/age/key.txt && "
                "chown root:root /etc/age/key.txt"
            ],
        )

    # 3) Derive the pubkey if missing. `age-keygen -y` reads a private key file
    #    and prints its public key. Mode 0444 (world-readable — public keys
    #    are not sensitive). We use host.get_fact(File, ...) to skip the shell
    #    op entirely on subsequent runs (clean "No change" rather than a
    #    no-op shell command logged as "Success").
    pubkey_present = host.get_fact(File, path=_AGE_PUB) is not None
    if not pubkey_present:
        server.shell(
            name="secrets/age: derive /etc/age/key.pub from /etc/age/key.txt",
            commands=[
                "/usr/local/bin/age-keygen -y /etc/age/key.txt > /etc/age/key.pub && "
                "chmod 0444 /etc/age/key.pub && "
                "chown root:root /etc/age/key.pub"
            ],
        )

    # 4) Pull the pubkey back to the operator Mac. files.get is the pyinfra
    #    operation that copies remote → local. add_deploy_dir=False so the
    #    `dest` path is taken absolute (not joined with the deploy dir);
    #    create_local_dir=True so the tenants/<name>/ dir is created if a
    #    fresh checkout doesn't have it yet. files.get is content-aware:
    #    when the local file already matches the remote (the common case
    #    after first deploy), the operation reports "No change".
    cfg = get_tenant_config(host)  # noqa: F841 — kept for parity with other tasks
    local_target = _local_pubkey_dest(host)
    files.get(
        name=f"secrets/age: copy box pubkey to {local_target}",
        src=_AGE_PUB,
        dest=str(local_target),
        add_deploy_dir=False,
        create_local_dir=True,
    )
