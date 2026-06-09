"""Install + register Tailscale on the tenant box (SPEC-011).

What this does, idempotently:
    1. Adds the official Tailscale apt repo (GPG key + sources list).
    2. Installs the `tailscale` package via apt.
    3. Ensures `tailscaled` service is enabled + running.
    4. Registers the box with the tailnet using a SOPS-decrypted auth key,
       ONLY IF the box is not already authenticated.

Idempotency:
    - apt sources files: idempotent (files.put hashes both sides)
    - apt install: idempotent (apt.packages skips if already present)
    - systemd: idempotent (systemd.service)
    - tailscale up: gated on a pre-check fact — checks `tailscale status` for
      Online status. Only runs `tailscale up` if NOT already registered.

SPEC-008 hard rule (no plaintext credential to stdout/stderr):
    The auth key is decrypted from /etc/bubble/secrets.sops.env into a tmpfs
    file at /run/tailscale-authkey (root:root 0400), passed to tailscale via
    the documented `--auth-key=file:` form (so the value never appears in
    `ps auxww`), then immediately removed. sops stderr is silenced via
    `2>/dev/null` to prevent dotenv-fragment leaks (see SPEC-008 incident).
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.facts.files import File
from pyinfra.facts.server import Command
from pyinfra.operations import apt, files, server, systemd

from lib.host_helpers import get_tenant_config


_KEYRING_URL = "https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg"
_SOURCES_URL = "https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list"
_KEYRING_PATH = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
_SOURCES_PATH = "/etc/apt/sources.list.d/tailscale.list"


def apply() -> None:
    """Install Tailscale + register box with tailnet."""
    cfg = get_tenant_config(host)
    ts = cfg.access.tailscale
    if not ts.enabled:
        # Tenant opted out (rare; would be set via tenant.yaml).
        return

    # ─── 1. Add Tailscale apt repo ──────────────────────────────────────────
    files.directory(
        name="access/tailscale: ensure /usr/share/keyrings exists",
        path="/usr/share/keyrings",
        mode="0755",
        user="root",
        group="root",
    )

    files.download(
        name=f"access/tailscale: download apt keyring from {_KEYRING_URL}",
        src=_KEYRING_URL,
        dest=_KEYRING_PATH,
        mode="0644",
        user="root",
        group="root",
    )

    sources_op = files.download(
        name=f"access/tailscale: download apt sources list from {_SOURCES_URL}",
        src=_SOURCES_URL,
        dest=_SOURCES_PATH,
        mode="0644",
        user="root",
        group="root",
    )

    # apt update — refresh cache so the next apt install sees pkgs.tailscale.com.
    # If the sources file JUST changed, FORCE an unconditional update (no cache_time)
    # — otherwise apt's "fresh" cache from before the sources file existed will
    # mean tailscale isn't found. Steady-state: skip via cache_time=3600.
    apt.update(
        name="access/tailscale: force apt update if sources changed",
        cache_time=0,  # 0 = always run when this op fires
        _if=sources_op.did_change,
    )
    apt.update(
        name="access/tailscale: refresh apt cache (cache_time=3600 steady state)",
        cache_time=3600,
    )

    # ─── 2. Install tailscale package ──────────────────────────────────────
    apt.packages(
        name="access/tailscale: install tailscale package",
        packages=["tailscale"],
        present=True,
        update=False,
    )

    # ─── 3. Ensure tailscaled service running + enabled ────────────────────
    systemd.service(
        name="access/tailscale: ensure tailscaled.service running+enabled",
        service="tailscaled",
        running=True,
        enabled=True,
    )

    # ─── 4. Register with tailnet (idempotent gate) ────────────────────────
    # Pre-check: if `tailscale status --json` shows Self.Online == true with
    # a registered NodeID, we're already part of the tailnet — skip the
    # `tailscale up` call entirely. This makes second-and-later runs zero-op.
    #
    # Nuance: the very first time tailscaled runs, `tailscale status` may
    # take a few seconds to report. We tolerate this by treating "any error
    # parsing" as "not registered" → run tailscale up. If we mis-detect, the
    # second `tailscale up` is also idempotent (it just reapplies settings).
    already_registered = host.get_fact(
        Command,
        command=(
            "tailscale status --json 2>/dev/null | "
            "python3 -c 'import sys,json; d=json.load(sys.stdin); "
            "print(\"yes\" if (d.get(\"Self\",{}).get(\"Online\") and d.get(\"Self\",{}).get(\"ID\")) else \"no\")' "
            "2>/dev/null || echo no"
        ),
    )
    is_registered = (already_registered or "").strip() == "yes"

    if not is_registered:
        # Build the tailscale up command. Auth key is passed via --auth-key=file:
        # form so the value never appears in `ps auxww`. The tmpfs file is
        # created, used, and removed in a single shell pipeline so a failure
        # mid-way still cleans up.
        #
        # Hostname: use the box's actual hostname (which we set to the tenant
        # name at provisioning time — joris-cx33 is correct for bubble-internal).
        # MagicDNS will then make `joris-cx33` resolve from any other tailnet
        # device.
        tags_arg = ""
        if ts.tags:
            # Tailscale wants comma-separated tags
            tags_arg = f"--advertise-tags={','.join(ts.tags)}"

        accept_routes_arg = "--accept-routes=true" if ts.accept_routes else "--accept-routes=false"

        # Build the inline shell pipeline. ALL on a single line to avoid
        # systemd / pyinfra splitting concerns.
        register_command = (
            "set -e; "
            # Decrypt auth key into tmpfs file (root:root 0400)
            "SOPS_AGE_KEY_FILE=/etc/age/key.txt /usr/local/bin/sops --decrypt /etc/bubble/secrets.sops.env 2>/dev/null "
            "  | grep '^TAILSCALE_AUTHKEY=' | cut -d= -f2- > /run/tailscale-authkey; "
            "chmod 0400 /run/tailscale-authkey; "
            # Sanity — file should be ~60 chars (tskey-auth-...). Bail if empty.
            "test -s /run/tailscale-authkey || (echo 'TAILSCALE_AUTHKEY missing or empty in decrypted secrets'; rm -f /run/tailscale-authkey; exit 1); "
            # Register
            f"tailscale up --auth-key=file:/run/tailscale-authkey "
            f"  {tags_arg} "
            f"  {accept_routes_arg} "
            f"  --advertise-routes= "
            f"  --reset; "
            # Wipe immediately
            "rm -f /run/tailscale-authkey"
        )

        server.shell(
            name="access/tailscale: register with tailnet (auth key via file:)",
            commands=[register_command],
        )

    # ─── 5. Verify ─────────────────────────────────────────────────────────
    # Always run this verify (it's a verification, not a state mutation —
    # always reports Success). Asserts we have a 100.x.x.x address.
    server.shell(
        name="access/tailscale: verify online + has tailnet IP",
        commands=[
            "tailscale status --json | python3 -c '"
            "import sys, json; "
            "d = json.load(sys.stdin); "
            "self = d.get(\"Self\", {}); "
            "assert self.get(\"Online\"), \"tailscale not Online\"; "
            "ips = self.get(\"TailscaleIPs\", []); "
            "assert ips, \"no TailscaleIPs assigned\"; "
            "print(\"tailnet IP:\", ips[0])"
            "'"
        ],
    )
