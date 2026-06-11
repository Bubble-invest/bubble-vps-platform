"""Bubble VPS Platform — top-level deploy orchestrator.

Step 2: Linux hardening task end-to-end against linux_hosts.
Step 3: SOPS+age secrets layer (both halves of Phase D).
Step 4: Agent layer — Claude Code + Bun install, persona sync, settings.json,
        systemd unit driving claude-agent-<persona>.service, verification gate,
        legacy plaintext-leak cleanup.
Step 6a: Tailscale install + tenant join.
Tasks A/B/D: telegram_watchdog, security_audit (cron), restart-on-secrets-change.
Task C (SPEC-015): per-tenant phone_home daemon + central dashboard (conditional).

Order matters: hardening prepares the box, secrets prepares the encrypted
layer, agent consumes both, access lights up the tailnet, then we install
monitoring that depends on all of the above.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pin two roots on sys.path so absolute imports work no matter how pyinfra
# invokes us (pyinfra exec's deploy files via compile() + exec() with no
# __package__, so relative imports don't resolve):
#   - platform repo root → resolves `lib.*`
#   - the local `pyinfra/` directory → resolves `tasks.hardening.linux`
#     without colliding with the installed `pyinfra` package on PyPI.
_REPO_ROOT = Path(__file__).resolve().parent
_TASKS_ROOT = _REPO_ROOT / "pyinfra"
for p in (str(_REPO_ROOT), str(_TASKS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pyinfra import host  # noqa: E402  (the installed pyinfra package)
from pyinfra.operations import server  # noqa: E402

from tasks.access import (  # noqa: E402
    cloud_wiki_sync,
    phone_home,
    security_audit,
    tailscale,
    telegram_watchdog,
)
from tasks.agent import deploy as agent  # noqa: E402
from tasks.hardening import linux as hardening  # noqa: E402
# tasks/monitoring/__init__.py re-exports each submodule's `apply` under a
# friendly name (dashboard, wiki_compile_alerts, restic_backup, cache_sync,
# secrets_sweep, transcript_leak_scan). Import the ones we call directly. The
# bare `monitoring.<fn>()` calls in step 11 below previously referenced an
# UNBOUND `monitoring` name (NameError at deploy time) — import the functions
# explicitly here so the call sites resolve, matching dashboard/wiki_compile.
from tasks.monitoring import (  # noqa: E402
    cache_sync,
    dashboard,
    restic_backup,
    secrets_sweep,
    transcript_leak_scan,
    wiki_compile_alerts,
)
from tasks.secrets import deploy as secrets  # noqa: E402

# 1) Apply the Linux hardening profile (idempotent — zero changes against an
#    already-hardened host).
hardening.apply()

# 2) Apply the secrets layer (Phase D both halves — installs age+sops, generates
#    the per-tenant box keypair, ships encrypted blob, verifies decryption).
#    No-op when cfg.secrets is None or cfg.secrets.enabled=False.
secrets.apply()

# 3) Apply the agent layer (Step 4 — Claude Code + Bun + persona + settings.json
#    + systemd unit + verification gate + legacy plaintext cleanup). No-op when
#    cfg.secrets is opt-out (the agent layer can't run without the secrets
#    layer providing /etc/bubble/secrets.sops.env).
agent.apply()

# 4) Apply the access layer (Step 6a — Tailscale install + tenant join).
#    Lets the operator SSH the box via the tailnet (bypassing public-IP rate
#    limits) and prepares the mesh for Step 6b's phone-home daemon. No-op
#    when cfg.access.tailscale.enabled=False.
tailscale.apply()

# 5) Apply the Telegram plugin recovery watchdog (Task D — SPEC-013).
#    systemd-timed liveness check + auto-restart for the bun-based Telegram
#    plugin running INSIDE the agent service. systemd doesn't see the plugin
#    child crash; this watchdog does. Runs after tailscale because alerts
#    via direct curl assume external network egress is healthy. No-op when
#    cfg.secrets is opt-out (no decrypted env to read the bot token from).
telegram_watchdog.apply()

# 6) Apply the daily cloud security audit (Task B — SPEC-014).
#    systemd-timed daily 09:00 UTC oneshot that runs an 8-part audit (auth,
#    secrets, agent, CVEs, disk/mem, transcript-leak scan, claude version,
#    hetzner firewall) and posts a summary message to Telegram. Runs after
#    telegram_watchdog because it shares the same alert-via-direct-curl
#    pattern + the same chat_id (Joris's primary_telegram_user_id). No-op
#    when cfg.secrets is opt-out (no decrypted env to read the bot token).
security_audit.apply()

# 7) Apply the per-tenant phone-home telemetry daemon (Task C — SPEC-015).
#    systemd-timed every 5 min oneshot that POSTs operational metadata
#    (host, agent, telegram, tailscale, claude_code) to the central
#    dashboard. NEVER ships data content — only counters/booleans. Reads
#    PHONEHOME_TOKEN from the runtime env file; sends it in the curl
#    Authorization: Bearer header (NOT the URL — defense in depth).
#    Runs after security_audit because it (and the dashboard) are part of
#    the same monitoring layer; ordering is otherwise interchangeable.
phone_home.apply()

# 8) Apply the cloud-side wiki sync timer (Phase 5b — SPEC-020).
#    Reciprocal of the Mac-side wiki-github-sync cron. Every 30 min: pull
#    --rebase --autostash + commit local dirty + push back to
#    github.com/vdk888/bubble-shared-wiki. Auth via GITHUB_TOKEN sourced
#    from the runtime env file → GIT_ASKPASS credential helper (token
#    NEVER in URL or argv). Conflict-abort + Telegram alert on rebase
#    failure. Runs after phone_home because it shares the same systemd-
#    timed shape and the same alert-via-direct-curl pattern. No-op when
#    cfg.secrets is opt-out.
cloud_wiki_sync.apply()

# 8b) Wiki-compile failure alerting (2026-06-05, Rick). OnFailure Telegram
#     alert on the nightly compile + a daily freshness watchdog for the
#     silent 'never fired' case. Runs after cloud_wiki_sync (shares the
#     scripts dir + secrets opt-out shape).
wiki_compile_alerts.apply()

# 9) Apply the central dashboard (Task C — SPEC-015), CONDITIONALLY.
#    Only the dashboard-host tenant (v1: hardcoded bubble-internal) actually
#    installs the dashboard service. Other tenants no-op — their phone-home
#    daemons POST here over the tailnet. Multi-tenant later: replace the
#    hardcoded gate inside dashboard.apply() with a tenant.yaml schema flag.
dashboard.apply()

# 10) Hello message — preserved from Step 1 so the operator sees a friendly
#    confirmation at the end of the run. server.shell echoes don't mutate
#    state but DO get reported as "changed" by pyinfra (the command ran).
#    We accept this — the dogfood test only counts changes for the hardening
#    task module run directly via tests/integration/test_dogfood_hardening.sh.
server.shell(
    name=f"deploy completed for tenant {host.data.tenant_name}",
    commands=[
        f"echo 'deploy completed for tenant: {host.data.tenant_name}'",
        "echo \"Hostname: $(hostname)\"",
        "echo \"OS: $(uname -a)\"",
        "echo \"User: $(whoami)\"",
    ],
)

# 11) Platform monitoring services (idempotent — zero changes against
#     already-installed units). These are the re-exported `apply` functions
#     imported at the top (NOT a `monitoring` module — there is no such name).
restic_backup()
cache_sync()
secrets_sweep()
transcript_leak_scan()

# 12) Clone or update bubble-ops-loop (idempotent — git pull if exists, clone if not).
from pyinfra.operations import server as _server
_LOOP_REPO = "https://github.com/Bubble-invest/bubble-ops-loop.git"
_LOOP_DIR = "/home/claude/bubble-ops-loop"
_server.shell(
    name="Clone/update bubble-ops-loop",
    commands=[
        f"[ -d {_LOOP_DIR}/.git ] && git -C {_LOOP_DIR} pull --ff-only || git clone {_LOOP_REPO} {_LOOP_DIR}"
    ],
    _sudo=True,
    _sudo_user="claude",
)

# Install loop-backup (layer timers + watchdog)
_server.shell(
    name="Install loop-backup (layer timers + watchdog)",
    commands=[f"bash {_LOOP_DIR}/scripts/install-loop-backup.sh"],
    _sudo=True,
)

# Install boot-rearm (telegram plugin patch)
_server.shell(
    name="Install boot-rearm",
    commands=[f"bash {_LOOP_DIR}/scripts/install-boot-rearm.sh"],
    _sudo=True,
)
