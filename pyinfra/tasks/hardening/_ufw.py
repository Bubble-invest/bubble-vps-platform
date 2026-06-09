"""UFW hardening sub-module.

Installs ufw, sets default policies (deny incoming, allow outgoing), adds
`ufw limit 22/tcp` (rate-limited SSH), and ensures the daemon is enabled.

Idempotency strategy: query `ufw status verbose` ONCE via a pyinfra fact at
deploy start, then only emit `server.shell` mutation ops when the parsed state
differs from desired. `server.shell` always counts as "changed" once executed,
so the only way to score zero changes on a re-run is to skip the op entirely
when state already matches. We do that with `_if=` predicates that capture
booleans computed from the fact output.

UFW's own commands ARE idempotent for default policies (`ufw default deny
incoming` returns 0 when already deny), but `ufw limit 22/tcp` is NOT — it
appends a duplicate rule each invocation. The presence guard is mandatory.

Drift discovery (2026-05-08):
    On joris-cx33, `ufw status verbose` shows TWO entries for the rate-limit
    (one for IPv4, one for IPv6 — UFW splits automatically). Our presence
    check greps for `22/tcp` + `LIMIT`; if at least one row matches we skip
    the `ufw limit 22/tcp` invocation.
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.facts.server import Command
from pyinfra.operations import apt, server, systemd


def apply(ufw_cfg) -> None:
    """Apply UFW hardening.

    Args:
        ufw_cfg: UfwConfig (enabled, allow_ssh_from). v1 always opens 22/tcp
            with rate-limiting; CIDR-narrowing of allow_ssh_from is a v2 feature.
    """
    # 1) Install ufw — apt.packages is idempotent (no-op if already present).
    apt.packages(
        name="hardening/ufw: install ufw",
        packages=["ufw"],
        present=True,
        update=False,  # apt.update was already called at the top of linux.py
    )

    # 2) Read current UFW state ONCE. The `|| true` ensures the fact never
    #    fails when ufw is not yet installed (first-run scenario) — get_fact
    #    just returns empty in that case.
    ufw_status = host.get_fact(Command, command="ufw status verbose 2>/dev/null || true") or ""

    has_default_deny_incoming = "Default: deny (incoming)" in ufw_status
    has_default_allow_outgoing = "allow (outgoing)" in ufw_status
    has_limit_ssh = "22/tcp" in ufw_status and "LIMIT" in ufw_status
    is_active = "Status: active" in ufw_status

    # 3) Default policies — only run if not already in desired state.
    if not has_default_deny_incoming:
        server.shell(
            name="hardening/ufw: default deny incoming",
            commands=["ufw default deny incoming"],
        )
    if not has_default_allow_outgoing:
        server.shell(
            name="hardening/ufw: default allow outgoing",
            commands=["ufw default allow outgoing"],
        )

    # 4) Rate-limited SSH rule. Skip entirely if already present (re-running
    #    `ufw limit 22/tcp` appends duplicates).
    if not has_limit_ssh:
        server.shell(
            name="hardening/ufw: add limit 22/tcp",
            commands=[
                "ufw limit 22/tcp comment 'SSH (rate-limited: 6 conns/30s/IP)'"
            ],
        )

    # 5) Enable UFW only if not already active.
    if not is_active:
        server.shell(
            name="hardening/ufw: enable firewall",
            commands=["ufw --force enable"],
        )

    # 6) Ensure ufw.service survives reboot (this is fact-based via systemd
    #    facts — pyinfra reports no-change when already enabled+running).
    systemd.service(
        name="hardening/ufw: ensure ufw.service running+enabled",
        service="ufw.service",
        running=True,
        enabled=True,
    )
