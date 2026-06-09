"""Install age + sops binaries on the box (SPEC-006 §"What Step 3 deploys").

Why GitHub releases instead of apt:
    Ubuntu 24.04 ships sops 3.7.x and age 1.1.x — both lag the upstream
    feature/security cadence. We pin specific versions here (constants below)
    so deploys are reproducible and we know exactly what's running.

Idempotency strategy:
    pyinfra's `files.download(... sha256sum=...)` only re-downloads when the
    file is missing OR the existing content's hash doesn't match. Combined
    with `creates=` on `files.unarchive`, the second run is a no-op.

Pinned versions:
    age   v1.2.1 — stable since 2024, no CVEs
    sops  v3.10.2 — current as of 2026-05; supports modern age recipients
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.facts.files import Sha256File
from pyinfra.operations import files, server


# ─── Pinned versions + checksums (sha256) ───────────────────────────────────
# Computed locally on 2026-05-08 against the upstream release artifacts; sops
# checksum is also published by getsops at sops-v3.10.2.checksums.txt.

AGE_VERSION = "1.2.1"
AGE_TARBALL_URL = (
    f"https://github.com/FiloSottile/age/releases/download/v{AGE_VERSION}/"
    f"age-v{AGE_VERSION}-linux-amd64.tar.gz"
)
AGE_TARBALL_SHA256 = "7df45a6cc87d4da11cc03a539a7470c15b1041ab2b396af088fe9990f7c79d50"
AGE_TARBALL_REMOTE = f"/root/.cache/bubble/age-v{AGE_VERSION}-linux-amd64.tar.gz"
AGE_EXTRACT_DIR = "/root/.cache/bubble"  # tarball extracts to ./age/age + ./age/age-keygen
AGE_BINARY_PATH = "/usr/local/bin/age"
AGE_KEYGEN_BINARY_PATH = "/usr/local/bin/age-keygen"

SOPS_VERSION = "3.10.2"
SOPS_BINARY_URL = (
    f"https://github.com/getsops/sops/releases/download/v{SOPS_VERSION}/"
    f"sops-v{SOPS_VERSION}.linux.amd64"
)
SOPS_BINARY_SHA256 = "79b0f844237bd4b0446e4dc884dbc1765fc7dedc3968f743d5949c6f2e701739"
SOPS_BINARY_PATH = "/usr/local/bin/sops"


def apply() -> None:
    """Install age + sops on the box.

    Order: age first (needed to generate the box keypair), then sops.
    """
    # ── age ────────────────────────────────────────────────────────────────
    # 1) Make sure the cache dir exists for the tarball download.
    files.directory(
        name="secrets/binaries: ensure /root/.cache/bubble exists",
        path=AGE_EXTRACT_DIR,
        mode="0700",
        user="root",
        group="root",
        present=True,
    )

    # 2) Download tarball (idempotent — sha256 match = no-op).
    files.download(
        name=f"secrets/binaries: download age v{AGE_VERSION} tarball",
        src=AGE_TARBALL_URL,
        dest=AGE_TARBALL_REMOTE,
        sha256sum=AGE_TARBALL_SHA256,
        user="root",
        group="root",
        mode="0600",
    )

    # 3) Extract — `creates=` makes this a no-op once the binary exists.
    files.unarchive(
        name=f"secrets/binaries: extract age v{AGE_VERSION} tarball",
        src=AGE_TARBALL_REMOTE,
        dest=AGE_EXTRACT_DIR,
        remote_src=True,
        creates=f"{AGE_EXTRACT_DIR}/age/age",
    )

    # 4) Move age + age-keygen into /usr/local/bin (root:root 0755). We compute
    #    the source sha256 once on the box (after extraction) and compare to
    #    the destination's current sha256. If they match, skip the install
    #    entirely — keeps the pyinfra "Changed" count clean on re-runs.
    extracted_age_path = f"{AGE_EXTRACT_DIR}/age/age"
    extracted_age_keygen_path = f"{AGE_EXTRACT_DIR}/age/age-keygen"

    src_age_sha = host.get_fact(Sha256File, path=extracted_age_path)
    dst_age_sha = host.get_fact(Sha256File, path=AGE_BINARY_PATH)
    if src_age_sha is None or src_age_sha != dst_age_sha:
        server.shell(
            name=f"secrets/binaries: install age v{AGE_VERSION} to {AGE_BINARY_PATH}",
            commands=[
                f"install -o root -g root -m 0755 {extracted_age_path} {AGE_BINARY_PATH}"
            ],
        )

    src_keygen_sha = host.get_fact(Sha256File, path=extracted_age_keygen_path)
    dst_keygen_sha = host.get_fact(Sha256File, path=AGE_KEYGEN_BINARY_PATH)
    if src_keygen_sha is None or src_keygen_sha != dst_keygen_sha:
        server.shell(
            name=f"secrets/binaries: install age-keygen v{AGE_VERSION} to {AGE_KEYGEN_BINARY_PATH}",
            commands=[
                f"install -o root -g root -m 0755 {extracted_age_keygen_path} {AGE_KEYGEN_BINARY_PATH}"
            ],
        )

    # 5) Sanity check: age --version. Doesn't change state but it'll fail loudly
    #    if the binary is corrupt — fail-fast before we lean on it later.
    server.shell(
        name=f"secrets/binaries: verify age v{AGE_VERSION} executable",
        commands=[
            f"{AGE_BINARY_PATH} --version | grep -qE 'v{AGE_VERSION}|^{AGE_VERSION}'"
        ],
    )

    # ── sops ───────────────────────────────────────────────────────────────
    # sops ships as a single binary, no tarball needed. Download direct to
    # /usr/local/bin/sops with the desired mode. files.download with sha256sum
    # is idempotent — re-runs are no-ops once the hash matches.
    files.download(
        name=f"secrets/binaries: install sops v{SOPS_VERSION}",
        src=SOPS_BINARY_URL,
        dest=SOPS_BINARY_PATH,
        sha256sum=SOPS_BINARY_SHA256,
        user="root",
        group="root",
        mode="0755",
    )

    server.shell(
        name=f"secrets/binaries: verify sops v{SOPS_VERSION} executable",
        commands=[
            f"{SOPS_BINARY_PATH} --version | grep -qE '{SOPS_VERSION}'"
        ],
    )
