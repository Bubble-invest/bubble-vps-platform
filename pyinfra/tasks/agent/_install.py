"""Install Node.js, Bun, and Claude Code CLI on the box (SPEC-007 §"What
Step 4 deploys").

Idempotency strategy:
    - Node.js: NodeSource setup script + apt install only when `node --version`
      is missing or doesn't satisfy the configured major version.
    - Bun: official installer downloads to ~/.bun. We `test -x` and skip if
      already present.
    - Claude Code: `npm install -g @anthropic-ai/claude-code` is itself
      idempotent (no-op when already installed at the same version), but we
      gate it behind a `which claude` fact so re-runs don't shell out.

The reading of `cfg.agent.install` makes the major-version configurable from
tenant.yaml (default "22"). Bun installs to /home/claude/.bun (the same path
the systemd unit puts on PATH via `/home/claude/.bun/bin`).
"""

from __future__ import annotations

from pyinfra import host
from pyinfra.facts.server import Command, Which
from pyinfra.operations import apt, server

from lib.host_helpers import as_claude


# NodeSource APT repo bootstrap. We do this only once — afterwards apt
# manages updates as part of the unattended-upgrades cycle.
_NODESOURCE_KEYRING = "/usr/share/keyrings/nodesource.gpg"
_NODESOURCE_LIST = "/etc/apt/sources.list.d/nodesource.list"


def _node_major(host_) -> int | None:
    """Return the installed Node major version, or None if not installed.

    `node --version` outputs e.g. `v22.22.2`. We pull the leading digit
    sequence after `v`. Returns None on any parse failure.
    """
    raw = host_.get_fact(Command, command="node --version 2>/dev/null || true")
    if not raw or not raw.startswith("v"):
        return None
    try:
        return int(raw.lstrip("v").split(".", 1)[0])
    except (ValueError, IndexError):
        return None


def apply() -> None:
    """Ensure Node.js, Bun, and Claude Code are installed."""
    # Read cfg lazily — we don't need it for every sub-step but it pins the
    # tenant-driven version configuration.
    from lib.host_helpers import get_tenant_config
    cfg = get_tenant_config(host)
    desired_major = int(cfg.agent.install.nodejs_version)

    # ── Node.js ────────────────────────────────────────────────────────────
    # Only run the NodeSource bootstrap when the installed Node major version
    # doesn't match. This avoids every deploy scribbling fresh apt sources.
    current_major = _node_major(host)
    if current_major != desired_major:
        # NodeSource v22 setup. Their setup_22.x script imports their GPG key
        # and writes /etc/apt/sources.list.d/nodesource.list. We do it manually
        # for idempotency control.
        server.shell(
            name=f"agent/install: bootstrap NodeSource v{desired_major} apt repo",
            commands=[
                # 1) Fetch + install GPG key (overwrites if changed; keyring
                #    name pins location for dpkg's apt source line).
                "curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | "
                f"gpg --dearmor -o {_NODESOURCE_KEYRING}",
                # 2) Write sources.list entry.
                f"echo 'deb [signed-by={_NODESOURCE_KEYRING}] "
                f"https://deb.nodesource.com/node_{desired_major}.x nodistro main' > "
                f"{_NODESOURCE_LIST}",
                # 3) Update apt indexes for the new repo.
                "apt-get update",
            ],
            # Writes /usr/share/keyrings/nodesource.gpg +
            # /etc/apt/sources.list.d/nodesource.list (root-owned) and runs
            # apt-get update (root-only). The deploy connects AS claude →
            # escalate. ROOT op → `_sudo=True` (no _sudo_user).
            _sudo=True,
        )
        apt.packages(
            name=f"agent/install: install nodejs (NodeSource v{desired_major})",
            packages=["nodejs"],
            present=True,
            update=False,
            # apt install is root-only → `_sudo=True`.
            _sudo=True,
        )

    # ── Bun ────────────────────────────────────────────────────────────────
    # Bun ships as a self-installing binary. It lives under /home/claude/.bun
    # so the install runs as the claude user (uses _su_user). Re-running the
    # installer overwrites with the same version idempotently — but we still
    # gate behind a presence fact to avoid the network round-trip on every
    # deploy.
    bun_present = host.get_fact(
        Command,
        command="test -x /home/claude/.bun/bin/bun && echo yes || echo no",
    )
    if bun_present and bun_present.strip() != "yes":
        server.shell(
            name="agent/install: install Bun (curl | bash from bun.sh)",
            commands=[
                # Run AS claude so $HOME resolves to /home/claude (bun installs
                # to ~/.bun). pyinfra connects as claude already; bare
                # `su - claude` would self-su-prompt for a password and abort.
                # The `curl ... | bash` pipeline must run wholly in the claude
                # shell (not just curl), so wrap it in `sh -c '...'` before
                # as_claude — otherwise `sudo -n -u claude` (root fallback)
                # would only run curl as claude and pipe to a root bash.
                as_claude("sh -c 'curl -fsSL https://bun.sh/install | bash'"),
            ],
        )

    # ── Claude Code CLI ────────────────────────────────────────────────────
    # `npm install -g @anthropic-ai/claude-code` is the official install path
    # (per https://code.claude.com/docs/en/installation). It's idempotent —
    # `npm install -g` is a no-op when the package is already at latest. We
    # still presence-check first so the deploy doesn't always show "changed".
    claude_present = host.get_fact(Which, command="claude")
    if not claude_present:
        server.shell(
            name="agent/install: install Claude Code via npm -g",
            commands=[
                "npm install -g @anthropic-ai/claude-code",
            ],
            # `npm install -g` writes to the NodeSource global prefix
            # /usr/lib/node_modules + symlinks the binary into /usr/bin/claude
            # (root-owned, see _CLAUDE_BIN in _systemd_unit.py). The deploy
            # connects AS claude → escalate. ROOT op → `_sudo=True`.
            _sudo=True,
        )

    # Sanity probe — if any of the three above produced a corrupt binary,
    # this fails loud with the version output truncated. No secrets involved.
    server.shell(
        name="agent/install: verify node / bun / claude executables present",
        commands=[
            "node --version > /dev/null",
            "test -x /home/claude/.bun/bin/bun",
            "claude --version > /dev/null",
        ],
    )
