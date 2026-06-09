# SPEC-020 — Cloud-side wiki sync (Phase 5b)

**Status:** v1.0
**Author:** Lab (rnd)
**Date:** 2026-05-09
**Depends on:** Phase 5a-prep done (Mac-side `wiki-github-sync` cron pushing every 30 min); Step 6a done (Tailscale up); GITHUB_TOKEN in tenant's SOPS file
**Implements:** Phase 5b of the Bubble VPS Platform build plan — closes the bidirectional wiki-sync loop so Morty can read what Lab writes AND push his own additions back to Mac

---

## Purpose

Mirror the Mac-side `wiki-github-sync` cron pattern on the cloud box. Morty needs:

1. **Initial clone:** `git clone https://github.com/vdk888/bubble-shared-wiki` into `/home/claude/.claude/agent-memory/shared-wiki/` on the box (matches Mac-side path)
2. **Periodic sync:** every 30 min, `git pull --rebase --autostash` to receive Lab's edits + commit any Morty edits + push back
3. **Auth:** uses the GITHUB_TOKEN env var from the systemd EnvironmentFile (already injected — Phase D + secrets layer hands it to the agent process). For git pull/push, we configure git's credential helper to use the token via HTTPS basic auth (the canonical pattern for fine-grained PATs).

After this lands: when Lab edits a wiki page on Mac → wiki-github-sync pushes within 30 min → Morty's reciprocal sync pulls within next 30 min → Morty sees the edit. Same in reverse for Morty's edits.

Worst-case end-to-end latency: ~60 min (depends on cron alignment). Acceptable for cross-agent learning; we don't need real-time.

---

## Architecture

```
┌─ Mac ─────────────────────┐
│  wiki-github-sync.sh       │
│  every 30min via launchd  │
│  pull --rebase + push     │
│  Auth: SSH agent (existing)│
└────┬──────────────────────┘
     │ git push/pull
     ▼
┌─ GitHub: vdk888/bubble-shared-wiki ┐
└────┬──────────────────────────────┘
     │ git push/pull
     ▼
┌─ {{VPS_HOST}} ───────────────────────┐
│  cloud-wiki-sync.sh                │
│  every 30min via systemd timer     │
│  pull --rebase + push              │
│  Auth: HTTPS basic via GITHUB_TOKEN │
│  (from SOPS → /run/claude-agent/env)│
└────────────────────────────────────┘
```

---

## Implementation

### Bash sync script: `pyinfra/templates/cloud-wiki-sync.sh.j2`

Bash, ~80 LOC. Mirrors `~/.claude/scheduled-tasks/wiki-github-sync/sync.sh` (the Mac-side equivalent) but uses HTTPS+token auth instead of SSH.

Operations per tick:
1. Validate the wiki dir exists at `/home/claude/.claude/agent-memory/shared-wiki/`. If missing → clone it (initial bootstrap).
2. Single-instance lock at `/run/cloud-wiki-sync.lock` (prevent overlapping syncs).
3. `git pull --rebase --autostash` — capture exit code via `${PIPESTATUS[0]}` (see "Conflict-vs-transient-failure" below), on **real conflict** abort + Telegram alert; on **transient failure** log a WARN and exit 0 (next tick retries).
4. If working tree dirty: `git add -A && git commit -m "cloud-wiki-sync $(date -u): N files"`.
5. Compute ahead-of-origin count via `git rev-list --count @{u}..HEAD`. If >0: `git push`.
6. Cooldown / rate-limit handling: NONE for now (GitHub allows 5000 req/hr per token, we're at ~2 req/30min).

#### Conflict-vs-transient-failure detection (added 2026-05-12)

A `git pull --rebase` failure can mean two very different things:
- **Real conflict**: rebase started, hit a merge conflict, paused mid-flight. Leaves `.git/rebase-merge/` (or `.git/rebase-apply/` on older git configs). Requires human resolution → Telegram alert + exit non-zero.
- **Transient failure**: network blip, GitHub 5xx, auth glitch, DNS failure. `git fetch` errored BEFORE the rebase started. No paused state. Next 30-min tick will recover naturally → just log a WARN and exit 0.

The script MUST distinguish these. Two implementation rules:

1. **Use `${PIPESTATUS[0]}` to capture git's exit code through the `| logger` pipe.** With `set -o pipefail`, an `if ! git ... | logger; then $?; fi` block correctly enters the failure branch (pipefail makes the pipe truthy-fail), BUT `$?` inside the block reflects the if-conditional, NOT the pipe stages. PIPESTATUS is the only way to get git's real exit code. Use `${PIPESTATUS[0]}` IMMEDIATELY after the pipe — any intervening command clobbers it.

2. **Branch on the presence of `.git/rebase-merge/` or `.git/rebase-apply/`.** Their existence after a failure is the canonical conflict signal. Network/auth failures never create these dirs because git aborts before touching the index.

Historical bug (2026-05-12): the original script used `if ! git pull ... | logger; then rebase_rc=$?` and unconditionally fired the Telegram conflict alert on any pull failure. A transient `Connection reset by peer` from github.com at `2026-05-12T00:38:56Z` (and an earlier one at `00:08:40Z`) generated false-positive "rebase conflict" alerts. Fixed by capturing `PIPESTATUS[0]` and branching on the paused-rebase dirs.

GITHUB_TOKEN handling per SPEC-008:
- Read from `/run/claude-agent/env` via `awk -F= '/^GITHUB_TOKEN=/{print $2; exit}'` into shell var `$TOKEN`
- Configure git to use it via `git config credential.helper '!f() { echo "username=x-access-token"; echo "password=$TOKEN"; }; f'` — this is GitHub's documented pattern for fine-grained PAT in HTTPS git
- `unset TOKEN` at end of script
- The credential helper persists in `.git/config` BUT only contains the literal string `$TOKEN` — it's resolved from env at git-call time. So no token-in-git-config-file leak.

Telegram alerts on conflict: same pattern as the Mac-side script + the Telegram-watchdog. Direct curl, captures bot token from env, posts to {{OPERATOR}}'s chat ID.

### systemd timer + service: `pyinfra/templates/cloud-wiki-sync.{timer,service}.j2`

```ini
# cloud-wiki-sync.timer
[Unit]
Description=Bidirectional wiki sync to GitHub (every 30 min)

[Timer]
OnBootSec=2min       # delay first sync 2 min after boot to let other services settle
OnUnitActiveSec=30min
Unit=cloud-wiki-sync.service

[Install]
WantedBy=timers.target
```

```ini
# cloud-wiki-sync.service
[Unit]
Description=Cloud wiki sync (one-shot per tick)
After=network-online.target

[Service]
Type=oneshot
User=claude  # NOT root — wiki dir is owned by claude
ExecStart=/home/claude/scripts/cloud-wiki-sync.sh
EnvironmentFile=-/run/claude-agent/env  # for GITHUB_TOKEN + TELEGRAM_BOT_TOKEN
StandardOutput=journal
StandardError=journal
```

Note: NO sudoers needed — claude owns the wiki dir, doesn't need elevated access.

### pyinfra task: `pyinfra/tasks/access/cloud_wiki_sync.py`

Operations:
1. Ensure `/home/claude/scripts/` exists (already from Task D, but idempotent dir is fine)
2. Initial clone if missing:
   ```python
   server.shell(
       name="access/cloud_wiki_sync: initial clone if /home/claude/.claude/agent-memory/shared-wiki/ missing",
       commands=[
           "test -d /home/claude/.claude/agent-memory/shared-wiki/.git || ("
           "  TOKEN=$(awk -F= '/^GITHUB_TOKEN=/{print $2; exit}' /run/claude-agent/env); "
           "  test -n \"$TOKEN\" || { echo 'GITHUB_TOKEN missing in env file'; exit 1; }; "
           "  mkdir -p /home/claude/.claude/agent-memory; "
           "  cd /home/claude/.claude/agent-memory; "
           "  git clone https://x-access-token:$TOKEN@github.com/vdk888/bubble-shared-wiki shared-wiki; "
           "  unset TOKEN; "
           ")"
       ],
       _sudo=True,  # need sudo to write to /home/claude as a different process
       _sudo_user="claude",  # but switch to claude user (not root)
   )
   ```
   Wait — this has token-in-URL exposure. URLs persist in `.git/config` after clone. BAD pattern.

   Better: clone WITHOUT token, then configure credential helper that reads token from env at request time:
   ```python
   server.shell(
       commands=[
           "test -d /home/claude/.claude/agent-memory/shared-wiki/.git || ("
           "  cd /home/claude/.claude/agent-memory; "
           "  git clone https://github.com/vdk888/bubble-shared-wiki shared-wiki && "
           "  cd shared-wiki && "
           "  git config credential.helper '!f() { echo \"username=x-access-token\"; echo \"password=$GITHUB_TOKEN\"; }; f'; "
           ")"
       ],
       _sudo=True,
       _sudo_user="claude",
   )
   ```
   The credential helper's `$GITHUB_TOKEN` is interpolated at git-call time from the calling process's env. The token is NEVER in `.git/config` (the helper line just contains the literal string `$GITHUB_TOKEN`).

   But INITIAL clone needs the token to authenticate. Two-step:
   - First, set `GIT_ASKPASS` env var to a small helper script that echoes the token
   - Run `git clone https://github.com/...` with `GIT_TERMINAL_PROMPT=0`
   - Git calls the helper, gets the token, clones, the URL stays clean

   This is the cleanest: a dedicated `git-credential-helper.sh` script lives at `/home/claude/scripts/git-credential-helper.sh` and is referenced by git.

3. Drop the wiki-sync script template
4. Drop the git-credential-helper script template
5. Drop systemd timer + service
6. systemctl daemon-reload (gated)
7. systemctl enable --now cloud-wiki-sync.timer

### Token handling: `pyinfra/templates/git-credential-helper.sh.j2`

Tiny helper, ~10 LOC:
```bash
#!/usr/bin/env bash
# Git credential helper. Reads GITHUB_TOKEN from env, formats per git's
# credential-helper protocol (username=value\npassword=value).
# Used via: git config credential.helper '/home/claude/scripts/git-credential-helper.sh'
echo "username=x-access-token"
echo "password=${GITHUB_TOKEN:-}"
```

Mode 0750 owner claude:claude. The script doesn't echo the token to stdout in a logged way — `git` reads it via the credential protocol and uses it for the HTTPS request. Token never appears in `ps`, never in journals.

---

## SPEC-008 hard rule compliance

- GITHUB_TOKEN read from `/run/claude-agent/env` (already-tmpfs-protected)
- Script captures into `$TOKEN` shell var, never echoes
- Git's credential helper reads `$GITHUB_TOKEN` from env at request time — token never persisted to `.git/config`
- For initial clone: GIT_ASKPASS pattern keeps token out of URL
- The wiki repo URL (`https://github.com/vdk888/bubble-shared-wiki`) is public-style and contains no auth info
- Telegram alert on conflict: bot token captured from env, used in HTTPS curl URL (brief ps exposure — same trade-off as Telegram-watchdog, acceptable for ops alerts)

---

## Idempotency

- Initial clone: `test -d ... || (...)` guard. Re-runs after first successful clone are no-ops.
- systemd unit drops: pyinfra hash-based idempotency (no-change if file content unchanged).
- Timer enable: `systemctl enable --now` is idempotent.

---

## Test plan

### Static tests in `lib/test_cloud_wiki_sync.py` (new file)

1. `test_sync_script_no_plaintext_credential_in_template` — render template, grep for any token-prefix patterns
2. `test_sync_script_unsets_token_after_use` — `unset TOKEN` after every curl using `$TOKEN`
3. `test_sync_script_uses_credential_helper_not_url_token` — assert script does NOT contain `https://x-access-token:` or `https://*:*@github.com` patterns (those would persist in `.git/config`)
4. `test_credential_helper_uses_x_access_token_username` — assert helper script's username is "x-access-token" (GitHub's documented value for fine-grained PAT HTTPS auth)
5. `test_systemd_timer_renders` — golden compare
6. `test_systemd_service_runs_as_claude_not_root` — assert User=claude in service unit
7. `test_pyinfra_module_uses_initial_clone_guard` — assert clone op has `test -d ... || (...)` pattern

Goldens at `lib/golden/access/cloud-wiki-sync.{timer,service}`.

### Integration test (manual on box)

After deploy:
1. `ssh hetzner 'sudo -u claude ls -la /home/claude/.claude/agent-memory/shared-wiki/' | head -10` → wiki tree present
2. `ssh hetzner 'sudo -u claude git -C /home/claude/.claude/agent-memory/shared-wiki/ remote -v'` → origin matches `vdk888/bubble-shared-wiki`
3. `ssh hetzner 'systemctl is-active cloud-wiki-sync.timer'` → active
4. `ssh hetzner 'sudo systemctl start cloud-wiki-sync.service; sudo journalctl -u cloud-wiki-sync.service -n 5 --no-pager'` → "ok" log
5. **Bidirectional smoke test** (skip — costs Mac/cloud round-trip, accept on the next cron tick)

---

## Acceptance criteria

Phase 5b done when:
1. ✅ Wiki cloned on box at `/home/claude/.claude/agent-memory/shared-wiki/`
2. ✅ `cloud-wiki-sync.timer` active + enabled
3. ✅ Manual run reports "ok" in journal
4. ✅ Git config uses credential helper, NOT token-in-URL
5. ✅ 7 new static tests pass
6. ✅ All previous tests still pass
7. ✅ pyinfra deploy idempotent (clone-if-missing skips on re-run)
8. ✅ Deploy logs grep clean for credential prefixes (`github_pat_`, `tskey-`, `sk-ant-oat01-`, `8350575119:`)

---

## Out of scope

- Multi-tenant wiki sync (only bubble-internal hosts the dashboard for v1; future tenants might have their own private wikis or read-only access to ours)
- Conflict resolution beyond "abort + alert" (auto-merging would corrupt content)
- Branch other than main
- LFS support (we don't have any LFS files in the wiki)
