# VPS GitHub wrappers — bubble-gh + bubble-git

**Path:** `/usr/local/bin/bubble-gh`, `/usr/local/bin/bubble-git` (both root:root 0755)
**Installed:** 2026-05-25
**Why:** {{OPERATOR}} flag msg 3208 — "Ok to use the GitHub CLI mentioned as option b in infra note".

## What they do

Both wrappers call the existing credential helper (`/usr/local/bin/bubble-gh-credential-helper.sh`) via sudo to mint a fresh GitHub App installation token (9-min TTL), then exec their underlying tool with that token injected.

- `bubble-gh <args>` → mints token → `GH_TOKEN=<tok> exec gh <args>`
- `bubble-git <args>` → mints token → `git -c http.extraheader=AUTHORIZATION: Basic <b64> <args>`

## Why two wrappers (not just bubble-git)

- **`bubble-gh`** for API calls and convenience (issues, PRs, releases, `gh api`, etc.) — `gh` reads `GH_TOKEN` env cleanly with no negotiation, so the first request always succeeds.
- **`bubble-git`** for actual git operations (push/pull/fetch/clone) — uses `-c http.extraheader` because git's credential.helper protocol only kicks in on 401-retry, which makes private repos fail with "not found" before auth is attempted.

## How install targeting works

Path-hint parsing scans argv for `*Bubble-invest/*` or `*{{GITHUB_OWNER}}/*` substring. Catches:
- `gh repo view owner/name`
- `gh repo clone owner/name`
- `gh api repos/owner/name/...` (the failing case at first deploy — now fixed)
- `--repo=owner/name`
- `bubble-git push https://github.com/owner/name`

For `bubble-git` only: if no argv hint, falls back to `git config --get remote.origin.url`.

Maps to:
- `Bubble-invest/*` → install 135214360 (Bubble-invest GitHub App)
- `{{GITHUB_OWNER}}/*` → install 134075326 ({{GITHUB_OWNER}} GitHub App)

## Tested 2026-05-25

```
bubble-gh repo view {{GITHUB_OWNER}}/bubble-rnd-workspace       → ✅
bubble-gh repo view Bubble-invest/bubble-ops-maya     → ✅
bubble-gh api repos/Bubble-invest/bubble-ops-maya/commits → ✅ (after pattern fix)
bubble-git push --dry-run origin onboarding/maya      → ✅ "Everything up-to-date"
```

## Security notes

- Tokens live only in process env for the lifetime of the exec'd command (≤ 9 min TTL anyway)
- Never persisted to disk
- `sudo -n` requires the existing `/etc/sudoers.d/bubble-cred` rule for claude user → no password prompt
- Helper itself is root-owned (defense in depth — claude can't tamper with the JWT signing code)

## Usage going forward

- All new scripts should use `bubble-gh` / `bubble-git` instead of inlining the credential helper or extraheader workaround
- Old `git` still works (helper invokes on 401-retry) but produces confusing "not found" errors on private repos first
- Document this in the infra note for the next operator
