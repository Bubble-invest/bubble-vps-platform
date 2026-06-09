# Wave 1 — Wire bubble-ops-loop install scripts into pyinfra provisioning

**Date:** 2026-06-08 · **Owner:** Rick (R&D) · **Status:** planning
**Trigger:** Joris msg 320 — "careful step by step and detailed planning per phase"

## Goal

Make `bubble-vps-platform` pyinfra deploy invoke the existing battle-tested
`bubble-ops-loop/scripts/install-*.sh` scripts instead of duplicating their
logic in separate pyinfra task modules. The install scripts are already
idempotent and live-verified on joris-cx33.

## Design constraint: Notion-optional

Per Joris: "we use Notion internally and clients may too, but it should be
optional and agnostic." Every feature that touches Notion must:
- Check for NOTION_API_KEY before attempting writes
- Skip gracefully with a log message when absent
- Not fail or degrade agent behavior when Notion is unavailable
- Accept alternative backends in the future (the interface is agnostic)

---

## Sub-tasks

### W1.1 — Refactor pyinfra monitoring tasks to invoke install scripts

**Current state:** I created standalone pyinfra task modules (restic_backup.py,
cache_sync.py, secrets_sweep.py, transcript_leak_scan.py) that duplicate the
logic of `scripts/install-*.sh`.

**Target:** Each pyinfra task calls its corresponding install script, which
handles template rendering, unit deployment, daemon-reload, and timer enablement.

**Scripts to wire:**
- `scripts/install-loop-backup.sh` → deploys loop-layer{1..4}.timer + loop-layer@.service + ops-loop-watchdog
- `scripts/install-sandbox.sh` → deploys bwrap+socat+AppArmor+managed-settings sandbox block
- `scripts/install-boot-rearm.sh` → patches telegram plugin for /loop boot re-arm
- Restic backup: already deployed via `scripts/morty-restic-setup.sh` (Phase 1 done)
- Cache sync, secrets sweep, transcript leak scan: deployed via bubble-ops-loop deploy scripts

**TDD gate:** For each script, verify it's idempotent (run twice, second run is no-op).

### W1.2 — Add sandbox step to pyinfra hardening

**Per BACKLOG.md mission:** "add the sandbox step to pyinfra/tasks/hardening/ so
every new tenant VPS auto-gets bwrap+socat+AppArmor+managed-settings."

**Implementation:**
- Add `_sandbox.py` to hardening tasks that calls `scripts/install-sandbox.sh`
- Wire into hardening `linux.py` apply() sequence
- Verify: sandbox engagement test passes after deployment

### W1.3 — Make Notion optional in L4 prompts

**Current state:** L4 prompts for tony, ben, maya reference Notion logbook
writes. `notion_logbook.py` already skips on missing key.

**Changes:**
- L4 prompts: "If NOTION_API_KEY is set, write to Notion logbook. Otherwise skip."
- Already partially done — the script checks for the key. Just need to ensure
  prompts don't imply Notion is mandatory.
- Add `--skip-notion` flag awareness to the dept scaffold template

### W1.4 — Make Notion optional in L1 prompts

**Current state:** Tony L1 reads Notion logbook (step 6).
**Changes:** If NOTION_API_KEY absent, skip the Notion read step. Use Telegram
history or other available sources instead.

### W1.5 — Clean up duplicated pyinfra templates

The 11 templates I added (restic-backup, cache-sync, secrets-sweep,
transcript-leak-scan) should be removed from pyinfra/templates/ since
the install scripts handle template rendering. Keep only templates that
pyinfra itself renders (sshd, ufw, etc.).

### W1.6 — Verify: deploy.py runs end-to-end in dry-run mode

Run `python3 deploy.py --dry-run` against a local inventory to verify
all imports resolve and all task apply() functions are callable without
runtime errors.

---

## Sequencing

1. W1.1 (refactor tasks → call install scripts)
2. W1.2 (sandbox in hardening)
3. W1.3 + W1.4 (Notion-optional prompts)
4. W1.5 (clean up duplicated templates)
5. W1.6 (end-to-end verification)

Each sub-task: plan → TDD test → implement → verify → commit.

---

## Files touched

- `pyinfra/tasks/monitoring/{restic_backup,cache_sync,secrets_sweep,transcript_leak_scan}.py` — refactor
- `pyinfra/tasks/hardening/_sandbox.py` — new
- `pyinfra/tasks/hardening/linux.py` — add sandbox call
- `pyinfra/templates/{bubble-restic-backup,bubble-cache-sync,secrets-tmp-sweep,transcript-leak-scan}.*.j2` — remove (moved to install scripts)
- `bubble-ops-loop/layers/4/PROMPT.md` (tony, ben, maya) — Notion-optional wording
- `bubble-ops-loop/layers/1/PROMPT.md` (tony) — Notion-optional wording
- `bubble-ops-loop/scripts/lib/scaffold.py` — Notion-optional in generated prompts
- `bubble-ops-loop/deploy/INSTALL.md` — add sandbox row
