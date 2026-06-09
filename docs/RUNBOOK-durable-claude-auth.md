# Runbook: Durable Box-Native Claude Re-Auth

> **Audience:** operator ({{OPERATOR}} or delegated). **Prereqs:** SSH access to the
> box as `claude`, the tenant's age key + `sops`/`age` on your workstation, and
> a browser to run `claude setup-token`.
>
> **When to use this:** the box's Claude agent is 401-ing (or about to), and you
> want to cut over from the brittle hand-ported `~/.claude/.credentials.json` to
> the durable `CLAUDE_CODE_OAUTH_TOKEN` model. Also use it ~once a year when the
> long-lived token nears expiry.

See `specs/SPEC-009-step4-addendum-claude-code-subscription.md` § *Durable
headless authentication* for the why. This runbook is the *how*.

---

## TL;DR (the cutover, end to end)

```bash
# 1. On your workstation, mint a LONG-LIVED token (opens a browser):
claude setup-token            # copy the printed token

# 2. Put it in the tenant blob as CLAUDE_CODE_OAUTH_TOKEN (GUI/hidden prompt, paste it):
./scripts/operator-set-secret.sh --tenant=bubble-internal --key=CLAUDE_CODE_OAUTH_TOKEN \
    --label="Paste the token from 'claude setup-token'"

# 3. Remove any hand-ported credentials file on the box so the env token wins:
ssh claude@<box> 'rm -f ~/.claude/.credentials.json'

# 4. Redeploy secrets + agent layer (pushes the updated blob, restarts the
#    service, runs the verify gate that asserts CLAUDE_CODE_OAUTH_TOKEN is wired):
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/secrets/deploy.py --sudo -y
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/agent/deploy.py --sudo -y

# 5. Confirm no 401 (see "Verify" below).
```

That's it. The rest of this doc explains each step, why the order matters, and
how to roll back.

---

## Why this order matters

`claude` **prefers `~/.claude/.credentials.json` when it is present** (verified
on the live box). So even with a perfect `CLAUDE_CODE_OAUTH_TOKEN` in the
environment, a leftover creds file keeps winning — and then expires and 401s.
The durable env token is *inert until the creds file is gone*. That is why
step 3 (remove the file) must happen, and must happen before/with the restart in
step 4.

The token from `claude setup-token` is **long-lived (~1 year) and refreshes
itself server-side** — unlike the Mac's interactive `accessToken`, which expires
in ~a day and is not refreshed in headless mode. So this is a once-a-year chore,
not a daily one.

---

## Detailed steps

### 1. Mint the long-lived token

On your workstation (anywhere you can open a browser):

```bash
claude setup-token
```

This runs the interactive browser auth and prints a long-lived token meant for
CI / headless use. Copy it. **Do not** paste it into shell history, a file you
forget to shred, or any chat. Treat it like any other tenant secret.

> This is the **only** step that needs a human + browser. Everything after is
> non-interactive plumbing.

### 2. Store it in the tenant SOPS blob

```bash
./scripts/operator-set-secret.sh --tenant=bubble-internal --key=CLAUDE_CODE_OAUTH_TOKEN \
    --label="Paste the token from 'claude setup-token'"
```

`operator-set-secret.sh` reads the value from a **native GUI password prompt**
(no terminal echo, no argv — so it can't leak via shell history or `ps auxww`)
and writes it with `sops --set`, so **only** the `CLAUDE_CODE_OAUTH_TOKEN` key
changes; every other ciphertext value is preserved. `umask 077` keeps the brief
plaintext window from ever being world-readable.

`CLAUDE_CODE_OAUTH_TOKEN` is already declared in the tenant's
`secrets.required_keys` (see `tenant.yaml`), so the value flows automatically
into each agent's decrypted runtime env file at service start — no schema change
needed.

> Remote prompt variant: if the operator who has the token sits at another Mac,
> `operator-set-secret.sh` supports `--remote-prompt=<ssh-host>` (the dialog pops
> on the remote Mac; the SOPS encrypt still happens locally). See the script
> header for details.

### 3. Remove the hand-ported credentials file (the shadow)

```bash
ssh claude@<box> 'rm -f ~/.claude/.credentials.json'
```

This is the load-bearing step. If a `~/.claude/.credentials.json` exists it
shadows the durable env token (see "Why this order matters"). The deploy **never
creates** this file (guarded — no `pyinfra/tasks/**` writes it; the token is
ported into the SOPS blob, not a creds file), so once you delete it, nothing
re-creates it.

> Multi-concierge boxes share one Linux user (`claude`) and therefore one
> `~/.claude/.credentials.json`. Removing it once covers all concierges.

### 4. Redeploy secrets, then the agent layer

```bash
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/secrets/deploy.py --sudo -y
TENANT=bubble-internal pyinfra inventory.py pyinfra/tasks/agent/deploy.py --sudo -y
```

Secrets deploy re-pushes the updated encrypted blob + validates `required_keys`.
Agent deploy re-decrypts into `/run/claude-agent[-<name>]/env`, restarts the
service(s), and runs the **verification gate**. The gate asserts (per concierge,
name-only — never the value) that `CLAUDE_CODE_OAUTH_TOKEN` is present in the
runtime env file. If that assertion fails, the deploy aborts before
`_cleanup_legacy` — old state is preserved for a roll-forward fix.

---

## Verify (no 401)

```bash
# The service is active and threw no errors in the last window:
ssh claude@<box> 'systemctl is-active claude-agent-morty.service'
ssh claude@<box> "journalctl -u claude-agent-morty.service --since '2 minutes ago' --priority=err --no-pager"

# The durable token NAME is in the runtime env (value never printed):
ssh claude@<box> 'sudo grep -q "^CLAUDE_CODE_OAUTH_TOKEN=" /run/claude-agent/env && echo present'

# There is NO shadowing credentials file:
ssh claude@<box> 'test ! -e ~/.claude/.credentials.json && echo "no creds file (good)"'
```

For a live end-to-end check, send the bot a Telegram DM and confirm a reply (the
agent only replies if its Claude API calls succeed, i.e. no 401).

> Token hygiene: every check above is **name-only** — `grep -q '^KEY='` matches
> the line and exits 0/1 without printing the value. Never `cat` the runtime env
> file or echo the token.

---

## Rollback

If the cutover misbehaves and you need the agent back fast:

1. Re-port the Mac credentials as the temporary band-aid (the old behavior):
   copy a fresh `~/.claude/.credentials.json` onto the box. It will win again
   (creds beat env) and tide you over for ~a day.
2. Investigate the durable token: re-run `claude setup-token`, re-set the blob
   key (step 2), confirm the deploy verify gate passes, then re-remove the creds
   file (step 3) and redeploy.

The durable model and the band-aid are not mutually destructive: dropping a
creds file always restores the old (daily) behavior because creds take
precedence. The cutover is simply "remove the creds file so the durable token
wins."

---

## Maintenance cadence

- **Yearly-ish:** before the `setup-token` token's ~1-year lifetime ends, repeat
  steps 1, 2, 4 (no creds file to remove if the cutover already happened).
- **Never again:** you should not need to port a daily Mac `.credentials.json`
  once the durable token is in place and no shadow file exists.
