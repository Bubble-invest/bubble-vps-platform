# REFINED PLAN — Bubble Cabinet

**Status**: refinement of `NORTH-STAR-BUBBLE-CABINET.md` (committed 2026-05-21).
**Author**: sub-agent of Rick (R&D), produced 2026-05-21 in parallel with a T-a-a-S refinement.
**Scope**: technical decomposition of the 3 sprints sketched in the north-star, plus 9 cross-cutting questions the north-star left under-specified.
**Out of scope**: writing any production code, modifying any other doc, pushing to git, talking to Telegram.

---

## 0. Executive summary

Reading the north-star against `bubble-vps-platform/`'s actual code base surfaces four big things:

1. **The "Linux container" framing hides three very different deployment platforms.** Mac (Docker Desktop), Windows (Docker Desktop or Podman), and Linux on-prem each have different volume semantics, performance ceilings, licensing footguns, and egress-restriction primitives. The 12h estimate assumes "we build once, ship everywhere"; in reality there are at least three QA matrices. Honest estimate: **~20–24h** for Sprints 1+2+3 done right, with a separate ~6h Sprint 0 for platform-spike validation before Sprint 1.

2. **The hard-blocking decision is git-bare vs Gitea sidecar** — and the right answer is **git-bare via `file:///srv/git-local/`**, not Gitea. Gitea adds an attack surface, a UI most clients won't use, an upgrade story we don't want to own, and ~120MB+ to the image. The client never reviews code via a PR UI; they review it via `claude` in the container ("show me what Sandra is about to push"). Sprint 2 should explicitly tag this in `git-guard`'s remote sniffing (skip token mint when `remote.url` starts with `file://`).

3. **Docker Desktop licensing is the second deal-breaker.** Most prospects who want zero-cloud are exactly the size class (>250 employees or >$10M revenue) that triggers Docker Desktop's paid tier. The north-star is silent. Bubble Cabinet must officially support **Podman Desktop + Docker Engine on Linux** as first-class targets, with Docker Desktop as the "if you have it" path. This rewrites the install.sh prereq check.

4. **The on-prem context kills 5 of the 9 pyinfra task layers we built for VPS.** UFW, fail2ban, unattended-upgrades, Tailscale, security-audit cron, telegram-watchdog, phone-home, cloud-wiki-sync — most of these either belong on the *host* (the client's IT runs them) or are replaced by Docker primitives. Section 6 maps each one. This is the genuine "framework is identical, just packaging differs" check — it's mostly true for the **agent layer + secrets layer**, mostly false for everything else.

The rest of this doc is the section-by-section refinement.

---

## 1. Dockerfile architecture decisions

### Base image: `debian:12-slim` (NOT Ubuntu 24.04)

The north-star says `ubuntu:24.04`. Three reasons to flip to `debian:12-slim`:

- **Size**: `ubuntu:24.04` is ~75MB compressed, `debian:12-slim` is ~30MB compressed. Both can host claude-code (Node 22) without trouble. For a USB-key delivery, every 50MB matters.
- **Package availability identical** for our needs (git, curl, openssh-client, python3.12, restic, jq, gettext all in Debian 12 main). SOPS + age = manual install from GitHub releases in both cases (neither ships in apt stable).
- **Security surface smaller**: Debian slim has no `snapd`, no `cloud-init`, no `landscape-client` — none of which we want anyway.

**Rejected: Alpine.** `claude-code` (and Node-based MCP plugins generally) hit musl-vs-glibc bugs that bite at the worst possible moment. The Bun installer also recommends glibc. Not worth the savings.

**Caveat**: if {{OPERATOR}} already prefers `ubuntu:24.04` because that's what we test on for VPS, the cost of being inconsistent is real (different package versions, different default user, different `apt` cache layout). **Recommendation: stay on `ubuntu:24.04-noble` for v1.0 to keep parity with the VPS code paths, plan a Debian-slim migration as v2.0 if image-size feedback warrants.** TODO: verify with {{OPERATOR}} which side of this tradeoff he prefers.

### Init system: bash supervisor script (NOT systemd, NOT s6-overlay)

The north-star is silent. Decision tree:

- **systemd-in-container**: ruled out. Requires `--privileged`, breaks Docker Desktop on Mac/Windows, terrible UX. Only viable if the client runs on bare Linux AND accepts privileged containers (which defeats half the point of "we packaged it safely").
- **s6-overlay**: viable but overkill. Adds ~5MB, an extra init layer, and a non-trivial learning curve for whoever debugs the container at 2am.
- **Bash supervisor**: simplest. One process per service (claude-agent, optional git-bare daemon, optional restic timer). Use `bash -c 'trap "kill 0" SIGTERM; <cmd1> & <cmd2> & wait'`. Loses fancy restart semantics but Docker's `restart: unless-stopped` covers the outer layer.

**Decision: bash supervisor for v1.0.** If we hit reliability issues (a sub-process dies silently, exit code lost), migrate to `tini` + a simple Python supervisor (~30 lines).

The VPS architecture uses systemd (`claude-agent-<persona>.service`). The container version replaces that with the bash supervisor + Docker's restart policy. This is the single largest "framework is not identical" delta. Acknowledge it; don't pretend.

### User model: `claude` non-root, no sudo in the container

The north-star says "User `claude` non-root + sudoers minimal". Refine: **no sudo at all** inside the container. The container is mono-purpose; whatever needs root happens at image-build time (Dockerfile RUN as root, then `USER claude`). Runtime needs root for nothing once age key is generated and the supervisor is launched as `claude`.

Volumes are bind-mounted with explicit `:rw` and matching UID 1000 (the `claude` user). On Mac via Docker Desktop, UID translation is handled by gRPCFUSE; on Linux this requires the operator's `install.sh` to either (a) run docker compose with `--user $(id -u):$(id -g)` or (b) `chown -R 1000:1000` the volume dirs at first boot.

**Justification**: Sandra inside the container cannot escalate. If she's compromised (prompt injection, malicious skill), blast radius = the container filesystem and the volumes she has access to. She cannot pivot to the host. **This is the entire selling point of containerization vs the VPS deploy.**

### Volumes: 5 named volumes, NOT bind mounts

The north-star lists 5 volumes (data, age, secrets, git, backups). Decision: **named volumes managed by Docker**, not bind mounts to host paths. Reasoning:

| Volume name (proposed) | Mount point | What lives there | Backup? |
|---|---|---|---|
| `cabinet-claude-home` | `/home/claude/.claude` | agent-memory, skills, transcripts, shared-wiki, settings.json, channels/telegram state | YES (restic) |
| `cabinet-age` | `/etc/age` | per-tenant box age private key (mode 0400) | YES (offline copy at install time, see backup-age-key.sh pattern) |
| `cabinet-secrets` | `/etc/bubble` | SOPS-encrypted secrets.sops.env | YES |
| `cabinet-git` | `/srv/git-local` | git-bare repos for each dept | YES |
| `cabinet-backups` | `/var/backups/bubble-restic` | restic repo (paradoxically inside the container BY DEFAULT, but configurable to a host bind for off-container backup) | NO (this IS the backup) |
| `cabinet-workspace` | `/home/claude/workspace` | bubble-ops-loop checkout + dept working trees | YES |

**Why named, not bind**: cross-platform headaches with bind mounts (case-sensitivity on Mac APFS vs Linux ext4, line-ending traps on Windows, UID mismatch on Linux). Named volumes are managed by Docker, queried via `docker volume inspect`, and survive `docker compose down` (NOT `down -v`).

**Backup implication**: restic must run *inside* the container with the named volumes mounted (so it sees them as filesystem paths). The restic repo itself can either live in `cabinet-backups` (= self-contained, no host disk needed, but loses if the host disk dies) OR be redirected via env var to a host bind mount (`/mnt/nas/bubble-restic`) for off-container durability. **Default to in-volume; document the bind-mount alternative in setup-local-backup.sh.**

### Network: bridge + outbound DNS allowlist

The north-star says `network_mode: bridge` + `extra_hosts` for api.anthropic.com and api.telegram.org. This is wrong-shape: `extra_hosts` adds entries to `/etc/hosts`, it doesn't restrict egress. To actually restrict egress to two domains, you need one of:

- **Docker's `--network` with iptables rules on the host** (Linux only, requires host root)
- **A sidecar `dnscrypt-proxy` or `coredns` container** that's the only DNS the cabinet sees, and that resolves only the allowed domains (works cross-platform; Sandra can't reach `evil.com` because DNS returns NXDOMAIN)
- **Outbound proxy enforcement** via env vars `HTTPS_PROXY` pointed at a `tinyproxy` sidecar with an allow-list (most robust; requires every client lib in the cabinet to honor the proxy, which they do for HTTP but not for raw TCP)

**Decision: v1.0 ships with default Docker bridge networking (= full egress). v1.1 ships the DNS-allowlist sidecar as opt-in.** Reason: getting v1.0 to a state where Sandra works reliably is hard enough; adding egress restriction on day 1 means every "Sandra can't reach an MCP server because we forgot to allowlist it" becomes a deployment-blocking bug. Sell "egress restricted" as a v1.1 feature.

The north-star promised "zero outbound network sauf api.anthropic.com" — this is a real promise to the client. **{{OPERATOR}} should know we're punting it to v1.1.** TODO: confirm with {{OPERATOR}} that v1.0 ships without egress restriction.

### Image distribution: GitHub Container Registry (NOT Docker Hub)

The north-star says `bubbleinvest/bubble-cabinet:vX.Y`. Refine:

- **Docker Hub**: 200-pull/6hr rate limit on anonymous pulls, requires creating a Docker Hub org, exposes our image listing publicly.
- **ghcr.io/bubbleinvest/bubble-cabinet:vX.Y**: no rate limit for authed pulls, free for public images, ties to our existing GitHub org. Tags pulled by the operator on USB-key install via `docker pull ghcr.io/bubbleinvest/bubble-cabinet:vX.Y` if they have internet, OR they install from the bundled tarball if airgapped.

**Decision: ghcr.io public, with USB-key tarball as offline fallback.** The USB-key includes the tarball `bubble-cabinet-v1.0.tar.gz` (output of `docker save`) + the loader `docker load -i ...`. install.sh tries `docker pull` first, falls back to `docker load` if no internet.

### Image size budget: target **< 1.5 GB compressed**

Breakdown sanity-check:
- Base `ubuntu:24.04-noble`: ~75MB
- Node 22 + npm: ~50MB
- Claude Code CLI: ~50MB (Anthropic CLI bundle)
- Bun: ~30MB
- Python 3.12 + base libs: ~50MB
- SOPS + age binaries: ~10MB
- Restic: ~20MB
- bubble-ops-loop checkout (skills, scripts, schemas, token-broker, git-guard): ~30MB
- Concierge persona seed (Sandra template): ~5MB
- Misc (git, curl, jq, yq, gettext, openssh-client, gpg): ~50MB

Total uncompressed estimate: ~370MB. Compressed: ~150MB.

The 1.5GB budget is generous; we should hit 500MB easily. If we need to add Playwright/Chromium for MCP browser tools (which the VPS deploy doesn't ship), it jumps to ~1.2GB. **Recommend: ship without browser by default; document the "add-browser variant" as a separate image tag `bubble-cabinet:v1.0-browser` for clients who need it.**

---

## 2. Verdict: git-bare local, not Gitea

### Why this matters

`bubble-ops-loop/scripts/bootstrap-dept.sh` (line 204) hard-codes `gh repo view` + `gh repo create` + `https://github.com/${FULL_NAME}.git`. The git-guard module (`bubble-ops-loop/git-guard/src/guard.py` lines 179-263) hard-codes the GitHub-App token mint via the broker. Neither works in an on-prem Bubble Cabinet without modification.

We have two options to fix this:

### Option A — git-bare with `file:///srv/git-local/<repo>.git` (RECOMMENDED)

```yaml
# docker-compose excerpt
services:
  cabinet:
    # ... agent runtime
    volumes:
      - cabinet-git:/srv/git-local
    environment:
      - BUBBLE_GIT_PROVIDER=local-bare
      - BUBBLE_GIT_BASE=file:///srv/git-local
```

That's it. No second service. `bootstrap-dept.sh` is patched to skip `gh repo view`/`gh repo create` when `BUBBLE_GIT_PROVIDER=local-bare` and instead do `git init --bare /srv/git-local/bubble-ops-<slug>.git`. `git-guard` is patched to detect `file://` URLs in the remote and no-op the broker mint (file-system writes are not protected by GitHub tokens).

**Pros**:
- Zero extra attack surface (no HTTP server, no web UI, no DB, no auth system).
- Zero RAM overhead.
- Backup story = back up the named volume.
- "Code review" happens inside the cabinet via `claude` reading the dept's working tree and explaining the diff to the owner on Telegram.
- The client's IT team can `git clone /var/lib/docker/volumes/cabinet-git/_data/bubble-ops-maya.git` from the host if they want a read-only inspection.

**Cons**:
- No web UI for the client to browse history (true cost: zero — the client doesn't want one, see north-star §"Anti-patterns à éviter").
- The "PR" concept disappears. Activation = a merge commit declarative push to main of the local-bare. This is fine; the PR was always primarily a record-keeping artifact.

### Option B — Gitea sidecar

```yaml
services:
  gitea:
    image: gitea/gitea:1.22
    volumes:
      - cabinet-gitea:/data
    environment:
      - GITEA_ROOT_URL=http://localhost:3000
    ports:
      - "127.0.0.1:3000:3000"
  cabinet:
    depends_on: [gitea]
    environment:
      - BUBBLE_GIT_PROVIDER=gitea
      - BUBBLE_GIT_BASE=http://gitea:3000/bubble
```

**Pros**:
- Web UI for client browsing.
- API mostly mirrors GitHub's, so `gh` CLI patched with a custom endpoint could work for many calls (gh-cli does support GHE; needs verification for Gitea).

**Cons**:
- +120MB image, +200MB RAM idle, +SQLite/PostgreSQL.
- Authentication system to admin (admin user, tokens, ACLs).
- Upgrade story: Gitea releases ~monthly; their migration system is good but not zero-touch.
- Attack surface: Gitea has had CVEs (CVE-2024-27734, CVE-2023-50445). Auto-upgrade is now another sprint.
- The client doesn't actually want a code-review UI — they want Sandra to *talk* to them about what changed.

### Decision

**Ship git-bare via `file:///srv/git-local/`.** Modifications needed:

1. `bootstrap-dept.sh` lines 101-260: branch on `${BUBBLE_GIT_PROVIDER:-github}`. When `local-bare`, replace `gh repo view`/`gh repo create` with `git init --bare`, set `REMOTE_URL=file:///srv/git-local/${REPO_NAME}.git`. Estimated +50 LoC.
2. `git-guard/src/guard.py` line 113 (`push` method): add early-return when `git config --get remote.<remote>.url` starts with `file://`. Run the path-check policy as before (clients still want governance), then `subprocess.run(["git", "push", remote, ref])` directly. Skip broker, skip token mint. Estimated +30 LoC.
3. `activate-dept.sh`: instead of `gh pr create`, do `git merge --no-ff onboarding/<slug>` on main of the local-bare via a temporary checkout. Same governance: log to `bubble-ops-audit.jsonl`, gate on the same policies.

**Test coverage**: extend `test_qa_e2e_full_walk` (per north-star Sprint 2 TDD) with `BUBBLE_GIT_PROVIDER=local-bare` to walk bootstrap → activate without any `gh` or `https://github.com` invocation. Add a unit test asserting `git-guard` invokes broker for `https://` remotes and skips it for `file://` remotes.

---

## 3. docker-compose.yml — concrete sketch

```yaml
version: "3.9"

services:
  cabinet:
    # Pull from ghcr.io with USB-key tarball fallback (handled by install.sh)
    image: ghcr.io/bubbleinvest/bubble-cabinet:${CABINET_VERSION:-v1.0}
    container_name: ${TENANT_NAME}-cabinet
    restart: unless-stopped
    init: true   # tini as PID 1 — handles zombie reaping
    environment:
      # Identity
      - TENANT_NAME=${TENANT_NAME}                       # e.g. "acme"
      - OWNER_DISPLAY_NAME=${OWNER_DISPLAY_NAME}         # e.g. "Marie Dupont"
      - OWNER_TELEGRAM_USER_ID=${OWNER_TELEGRAM_USER_ID} # numeric, allowlist gate
      - CONCIERGE_NAME=${CONCIERGE_NAME}                 # e.g. "Sandra"
      # Channels
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}         # FROM .env (sensitive)
      - TELEGRAM_ALLOWED_USER_IDS=${OWNER_TELEGRAM_USER_ID}
      # Claude auth — exactly ONE of these two:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}
      # Git mode
      - BUBBLE_GIT_PROVIDER=local-bare
      - BUBBLE_GIT_BASE=file:///srv/git-local
      # Backups
      - RESTIC_REPOSITORY=/var/backups/bubble-restic
      - RESTIC_PASSWORD_FILE=/etc/bubble/restic.pass     # generated by install.sh
      # Runtime
      - TZ=${TZ:-Europe/Paris}
      - PYTHONUNBUFFERED=1
    volumes:
      - cabinet-claude-home:/home/claude/.claude
      - cabinet-age:/etc/age
      - cabinet-secrets:/etc/bubble
      - cabinet-git:/srv/git-local
      - cabinet-backups:/var/backups/bubble-restic
      - cabinet-workspace:/home/claude/workspace
    # No ports published by default — Telegram is outbound-only.
    # If a console UI is added later, publish 127.0.0.1:8080 only.
    # ports: []
    networks:
      - cabinet-net
    healthcheck:
      test: ["CMD", "test", "-f", "/run/cabinet/healthy"]   # touched by supervisor
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
    deploy:
      resources:
        limits:
          memory: 4G
          cpus: "2.0"
        reservations:
          memory: 1G
          cpus: "0.5"
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

  # Optional: restic backup runner (could also be a cron INSIDE cabinet, kept
  # separate for blast-radius isolation — if cabinet OOMs mid-backup, restic
  # still completes).
  restic:
    image: ghcr.io/bubbleinvest/bubble-cabinet:${CABINET_VERSION:-v1.0}
    container_name: ${TENANT_NAME}-restic
    restart: unless-stopped
    depends_on:
      cabinet:
        condition: service_healthy
    entrypoint: ["/usr/local/bin/restic-supervisor.sh"]   # custom: cron-like daily run
    environment:
      - RESTIC_REPOSITORY=/var/backups/bubble-restic
      - RESTIC_PASSWORD_FILE=/etc/bubble/restic.pass
      - BACKUP_PATHS=/home/claude/.claude /etc/age /etc/bubble /srv/git-local /home/claude/workspace
      - BACKUP_CRON=0 3 * * *   # 03:00 local time
    volumes:
      - cabinet-claude-home:/home/claude/.claude:ro
      - cabinet-age:/etc/age:ro
      - cabinet-secrets:/etc/bubble:ro
      - cabinet-git:/srv/git-local:ro
      - cabinet-workspace:/home/claude/workspace:ro
      - cabinet-backups:/var/backups/bubble-restic
    networks:
      - cabinet-net

volumes:
  cabinet-claude-home:
  cabinet-age:
  cabinet-secrets:
  cabinet-git:
  cabinet-backups:
  cabinet-workspace:

networks:
  cabinet-net:
    driver: bridge
```

**Notes**:

- Total services: **2** (cabinet + restic). Both use the same image — restic isn't a separate image because we already need it inside cabinet for ad-hoc CLI, and shipping one image keeps download/USB delivery simple.
- Restic's `depends_on: condition: service_healthy` waits for cabinet's healthcheck before booting (no point backing up an empty volume on first boot).
- No `ports` published. If a future v1.x console exposes a web UI, it binds to `127.0.0.1:` only.
- `init: true` enables tini, solving the zombie-reaping problem of bash supervisor.
- Memory cap 4G: Claude Code peak observed ~2.5GB on heavy reasoning. 4G is comfortable; 2G is the floor under which we see OOM-kills.
- The `:ro` mounts on the restic service prevent accidental writes to volumes during backup; the cabinet keeps `:rw`.
- All env vars come from `.env` next to `docker-compose.yml`; no secrets live in the compose file.

---

## 4. install.sh decomposition

The north-star says "wrapper one-shot". Decompose into 9 phases. Idempotent throughout.

### Preflight (exit codes documented for support)

| Check | Failure exit | Why |
|---|---|---|
| `docker --version` returns ≥ 24.x | 10 | Older Docker has compose v1, breaks our v2 syntax |
| `docker compose version` returns ≥ 2.20 | 11 | We use `service_healthy` condition + `init: true` |
| `docker info` (daemon reachable) | 12 | Daemon must be running |
| `.env` exists at expected path | 20 | Operator must have filled it |
| `.env` has all required keys non-empty | 21 | Per the .env.template checklist |
| **Exactly one of** `ANTHROPIC_API_KEY` OR `CLAUDE_CODE_OAUTH_TOKEN` is set | 22 | Avoids ambiguity at agent runtime |
| `OWNER_TELEGRAM_USER_ID` matches `^[0-9]+$` | 23 | Catches "username vs ID" confusion |
| `TELEGRAM_BOT_TOKEN` matches `^[0-9]+:[A-Za-z0-9_-]+$` | 24 | Catches a copy-paste error early |
| Free disk space ≥ 10GB on Docker root | 30 | Image + volumes + backups need room |
| `getent passwd 1000` exists OR Mac/Windows (skipped) | 31 | UID alignment on Linux only |

### Prompts (with non-interactive fallbacks)

- **Generate age key now?** (Y/n) — default Y. Fallback in `BUBBLE_CABINET_NONINTERACTIVE=1`: Y.
- **Restic passphrase**: generate random (default) or paste your own. Non-interactive: generate.
- **Pull image vs load from tarball**: auto-detect — try `docker pull ghcr.io/...`, if it fails AND `bubble-cabinet-${CABINET_VERSION}.tar.gz` exists in script dir, `docker load`. Non-interactive: same logic.

### Sequence of operations

1. **Banner** with version, tenant name, target system.
2. Run preflight (exits with code + message on any failure).
3. **Source `.env`** (strict mode, fail on undefined required vars).
4. **Generate age key** if `cabinet-age` volume doesn't already contain one. Uses a one-shot helper container (`docker run --rm -v cabinet-age:/etc/age ${IMAGE} age-keygen -o /etc/age/key.txt && chmod 0400 ...`). Idempotent: if `/etc/age/key.txt` exists, skip.
5. **Generate restic passphrase** to `cabinet-secrets` volume at `/etc/bubble/restic.pass` (mode 0400). Idempotent.
6. **Pull or load image**.
7. **`docker compose up -d cabinet`** (NOT the whole compose yet — wait for cabinet healthy first).
8. **Wait for cabinet health** — poll `docker inspect --format='{{.State.Health.Status}}'` every 2s up to 5min. On timeout: dump last 50 lines of `docker logs`, exit 50.
9. **`docker compose up -d restic`** (now that cabinet is healthy, the backup sidecar can mount the volumes).
10. **Post-install message** (see UX section §8 below).
11. **Write `install.log`** to the script directory with timestamp + every operation outcome (for support).

### Idempotency

- Re-running install.sh: detects existing age key → skip generation. Detects healthy cabinet → skip waiting. `docker compose up -d` is itself idempotent. Net result on a healthy system: ~3s "no-op" output, "Cabinet already running. Sandra is online."
- Re-running after `docker compose down`: restarts cabinet + restic, no key regeneration, no data loss.
- Re-running after `docker compose down -v`: detects empty volumes, re-runs first-boot path. **Warns operator: "Data destroyed by `down -v`. Did you mean to restore from restic?"**

### Log file content

`install.log` is a flat text log with one line per operation:

```
[2026-05-21T14:32:01+02:00] INFO  preflight:docker-version PASS (24.0.7)
[2026-05-21T14:32:01+02:00] INFO  preflight:compose-version PASS (2.27.0)
[2026-05-21T14:32:02+02:00] INFO  env:loaded TENANT_NAME=acme OWNER=Marie Dupont
[2026-05-21T14:32:03+02:00] INFO  age:keygen SKIPPED (existing key in cabinet-age)
...
[2026-05-21T14:33:10+02:00] INFO  health:cabinet HEALTHY after 47s
[2026-05-21T14:33:11+02:00] DONE  install complete in 70s
```

No secrets in the log. Reviewable by the client's IT without exposure risk.

---

## 5. Cross-platform compatibility matrix

| Platform | Verdict | Notes | Tests needed |
|---|---|---|---|
| **Mac Apple Silicon + Docker Desktop** | WORKS-WITH-CAVEATS | gRPCFUSE volume perf OK for our workload (mostly text I/O). Sleep/wake cycles occasionally leave Docker daemon zombied — `docker compose restart` fixes it. Memory recommendation: allocate ≥ 6GB to Docker Desktop VM. | Pin: 1h sleep + wake + send Telegram msg → reply. Repeat 3x. |
| **Mac Intel + Docker Desktop** | WORKS-WITH-CAVEATS | Same as Apple Silicon. Older Intel Macs (pre-2019) marginal on RAM — 16GB system is the floor. | Same as Apple Silicon. |
| **Windows 11 + WSL2 + Docker Desktop** | WORKS-WITH-CAVEATS | Volumes inside WSL2 ext4 are fast; volumes bind-mounted from Windows NTFS are slow (5–10x). Force `wsl --set-version Ubuntu 2` and run install.sh from within WSL. CRLF traps: every `.sh` and `.env` must be LF-only — provide a `.gitattributes` with `* text=auto eol=lf`. | Pin: full install on a fresh WSL2 Ubuntu-22.04 distro. |
| **Windows 11 + Docker Desktop (no WSL2)** | NOT-RECOMMENDED | Hyper-V backend works but has known volume perf issues + zombie containers after Hyper-V VM suspend. | Skip; document as unsupported. |
| **Windows 11 + Podman Desktop** | WORKS-WITH-CAVEATS | Podman 5.x has good compose support; named volumes work. Caveat: Podman's healthcheck implementation has historically been spotty. **Sprint 1 should explicitly test the Podman path in CI.** | Pin: install + Sandra answers /start, restart container, conversation persists. |
| **Linux (Ubuntu 22.04 / 24.04 LTS)** | WORKS | Native, fastest, no UID translation gotchas (with the `chown 1000:1000` step). Recommended path for any client with a server. | Pin: full install on fresh Ubuntu 22.04 minimal. |
| **Linux (Debian 12)** | WORKS | Same as Ubuntu. | Same as Ubuntu. |
| **Linux (RHEL 9 / Rocky / Alma)** | WORKS-WITH-CAVEATS | Docker Engine works (Docker CE supported); Podman is the distro default and equally fine. SELinux in enforcing mode may need `:z` labels on volume mounts. | Pin: install on Rocky 9, both with Docker Engine and Podman. |

**Concrete test list for Sprint 0 (platform spike)** — before Sprint 1 starts:

1. `T-MAC-1`: Fresh M-series Mac, Docker Desktop 4.x, `./install.sh` to "Sandra answers /start" in ≤ 15 min.
2. `T-WIN-1`: Fresh Windows 11 + WSL2 + Docker Desktop, same scenario.
3. `T-WIN-2`: Fresh Windows 11 + Podman Desktop, same scenario.
4. `T-LIN-1`: Fresh Ubuntu 22.04 minimal, Docker Engine, same scenario.
5. `T-RHEL-1`: Fresh Rocky 9 with Podman, same scenario.
6. `T-SLEEP-1`: After install, sleep host 1h, wake, `restart`, Sandra resumes.
7. `T-RESTORE-1`: `docker compose down -v`, `./scripts/restore-from-restic.sh`, full recovery.

Sprint 0 alone is ~6h. The north-star didn't budget for this. **Honest estimate.**

---

## 6. VPS-to-Container task map

Going through `bubble-vps-platform/pyinfra/tasks/` directory by directory:

| pyinfra task | Status in Bubble Cabinet | Replacement |
|---|---|---|
| `hardening/_sshd.py` | **DROP** | No sshd inside container; host's IT runs sshd on the host. |
| `hardening/_ufw.py` | **DROP** | Docker network isolation replaces UFW for container. Host UFW is the client's IT problem. |
| `hardening/_fail2ban.py` | **DROP** | Same — host concern. |
| `hardening/_unattended.py` | **DROP** | `apt unattended-upgrades` doesn't apply inside a stateless container. Patching = rebuild image + redeploy. |
| `hardening/_swap.py` | **DROP** | Host concern. Document a "host prep" checklist. |
| `hardening/_ntp.py` | **DROP** | Docker shares host clock via VM (Mac/Win) or directly (Linux). |
| `secrets/_binaries.py` | **REPRODUCE in Dockerfile** | `apt install` + GitHub-release downloads of sops + age at image-build time. |
| `secrets/_age_setup.py` | **REPRODUCE in install.sh** | First-boot age-keygen into `cabinet-age` volume. Same mode-0400 invariant. The "copy pubkey back to operator Mac" step doesn't apply (the client IS the operator). |
| `secrets/_sops_deploy.py` | **PARTIAL** | The encrypted file ships INSIDE the image OR is generated by install.sh from .env (simpler for v1.0: install.sh writes `.env` values directly to `/etc/bubble/secrets.sops.env` after encrypting with the freshly-generated age key). The "no plaintext to stdout" invariant still applies. |
| `agent/_install.py` | **REPRODUCE in Dockerfile** | Image-build time — node + bun + claude-code globally installed. |
| `agent/_persona.py` | **REPRODUCE in install.sh** | First-boot copy of persona template (Sandra) into `cabinet-claude-home`, parameterized by `CONCIERGE_NAME` / `OWNER_DISPLAY_NAME` from .env. The pyinfra `rsync` becomes a `cp -R` + `envsubst` on the template files. |
| `agent/_settings.py` | **REPRODUCE in install.sh** | Generate `~/.claude/settings.json` inside the volume from a template. |
| `agent/_telegram_plugin.py` | **REPRODUCE in Dockerfile** | Install at image-build time; runtime state dir created at first boot. |
| `agent/_systemd_unit.py` | **REPLACE with bash supervisor** | The systemd unit is replaced by the container's `CMD` / `ENTRYPOINT` running the bash supervisor that exec's `claude --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official`. |
| `agent/_verify.py` | **PARTIAL — adapt to healthcheck** | Six-check gate becomes the healthcheck script that touches `/run/cabinet/healthy`. |
| `agent/_cleanup_legacy.py` | **DROP** | Container starts clean; no legacy plaintext leak to wipe. |
| `access/tailscale.py` | **DROP** | Tailscale is for OUR remote access to VPS tenants. On-prem clients don't want us on their tailnet. If they want our remote help, they invite us via their VPN. |
| `access/telegram_watchdog.py` | **REPRODUCE in bash supervisor** | The supervisor monitors the telegram plugin sub-process; restarts on `getWebhookInfo` failure. ~50 lines of bash replaces the pyinfra task + jinja template + systemd timer. |
| `access/security_audit.py` | **PARTIAL — recommend opt-in** | Daily 09:00 security audit makes sense; report goes to operator's Telegram (same chat as Sandra). Reduce scope: drop UFW/fail2ban/CVE checks (host concern); keep auth/secrets/agent/disk/transcript-leak checks. ~40% of original scope. |
| `access/phone_home.py` | **DROP for on-prem** | The whole point of on-prem is no telemetry to us. If a client wants central monitoring, they run their own dashboard. |
| `access/cloud_wiki_sync.py` | **DROP for on-prem** | No central wiki on-prem. Sandra's shared-wiki is local-only. |
| `monitoring/dashboard.py` | **DROP for on-prem** | Same reasoning. |

**Net result**: of the 20-ish pyinfra task modules, **~6 are reproduced in some form, ~14 are dropped or replaced.** The "framework is identical" claim in the north-star is true for the *Claude Code agent runtime* and *the SOPS+age secrets primitive*; it's false for everything operational. Sandra runs the same `bootstrap-dept.sh` as Morty does; the wrapper around her runs nothing like the VPS wrapper.

---

## 7. Docker Desktop licensing — reality check

### The rule

Docker Desktop is free for:
- Personal use
- Education (students + teachers)
- Open source projects
- Companies with ≤ 250 employees AND ≤ $10M annual revenue

It is **paid** (Business plan: $9/user/month, Pro: $5, Team: $9) for any company outside the above. The license is checked on a "honor system" basis but Docker Inc. has audited companies before.

### Implication for Bubble Cabinet prospects

The Bubble Cabinet target market is **companies wanting data sovereignty**. That correlates strongly with: banks, hospitals, law firms, government — all of whom are well over the 250-employee/$10M threshold.

**A client who installs Bubble Cabinet on a Windows or Mac workstation via Docker Desktop is technically in violation of Docker's license** unless they already have a paid Docker subscription.

### Recommendation

**Bubble Cabinet officially supports three engines, in priority order:**

1. **Docker Engine on Linux** (free, no licensing question) — primary target for on-prem servers. This is what we should optimize for.
2. **Podman Desktop on any OS** (free, MIT license, no commercial restrictions) — primary fallback for clients who want to develop/test on Mac or Windows without Docker licensing. Podman 5.x with `podman compose` (or `podman-compose`) handles our compose file. **Sprint 1 must include Podman validation tests.**
3. **Docker Desktop** (Mac/Windows, with the client's existing license) — supported, but documented as "you confirm you have a valid Docker subscription".

Concrete actions:

- The install.sh preflight detects which engine is in use (`docker info | grep "Server Version"` vs `podman info`). It does NOT block Docker Desktop, but it does log "Detected Docker Desktop — ensure your organization has a valid subscription per docker.com/pricing."
- The README-INSTALL.md has a paragraph: "Bubble Cabinet supports Docker Engine (recommended for servers), Podman Desktop (recommended for Mac/Windows workstations), or Docker Desktop (with your existing license)."

This is the kind of decision that, if we miss it, will lead a client legal team to NACK the install at the last minute. **Treat it as a deal-breaker if not addressed.** TODO: verify with {{OPERATOR}} that Bubble Invest is OK positioning Podman as an officially supported alternative.

---

## 8. The first 5 minutes after install

This is the demo moment that sells the product. Sketch the exact terminal output:

```
$ ./scripts/install.sh

╔═══════════════════════════════════════════════════════════════╗
║              Bubble Cabinet — installation v1.0               ║
║                Tenant: acme   Concierge: Sandra               ║
╚═══════════════════════════════════════════════════════════════╝

[1/9] Preflight checks ...
      ✓ docker 24.0.7
      ✓ docker compose v2.27.0
      ✓ daemon running
      ✓ .env complete (8 required keys)
      ✓ owner Telegram ID looks valid
      ✓ Telegram bot token looks valid
      ✓ 47 GB free on Docker root
      ✓ engine: Docker Engine (Linux)
[2/9] Loading .env ... OK
[3/9] Generating age key for cabinet-age volume ... OK (new key, fingerprint age1xx...)
[4/9] Generating Restic passphrase ... OK
[5/9] Pulling image ghcr.io/bubbleinvest/bubble-cabinet:v1.0 ...
      [████████████████████] 142 MB / 142 MB
      ✓ pulled
[6/9] Starting cabinet service ...
      ✓ container started
[7/9] Waiting for cabinet health ............... ✓ HEALTHY (47s)
[8/9] Starting restic backup sidecar ... ✓
[9/9] Wiring done.

╔═══════════════════════════════════════════════════════════════╗
║                  Sandra is online.                            ║
║                                                               ║
║  Open Telegram and start a chat with your bot:                ║
║      @bubblecabinet_acme_bot                                  ║
║                                                               ║
║  Send /start to Sandra. She will greet Marie Dupont and       ║
║  propose three actions.                                       ║
║                                                               ║
║  Logs:    docker logs -f acme-cabinet                         ║
║  Backups: scheduled daily at 03:00 (cabinet-backups volume)   ║
║                                                               ║
║  If something goes wrong:                                     ║
║      cat install.log                                          ║
║      docker compose logs                                      ║
║      see README-INSTALL.md §Troubleshooting                   ║
╚═══════════════════════════════════════════════════════════════╝

Install completed in 1m 12s.
```

The "wow it works" moment: when the operator opens Telegram, taps `/start`, and within ~2s Sandra replies:

> Bonjour Marie. Je suis Sandra, votre concierge Bubble Cabinet.
> Je peux vous aider à :
> 1️⃣ Accueillir un premier collègue agentique (Maya pour prospection, Ben pour finance, etc.)
> 2️⃣ Surveiller ce qui se passe sur votre serveur
> 3️⃣ Vous expliquer comment ça marche
> Par quoi on commence ?

The "uh-oh" detection: if `/start` doesn't get a reply within 30s, install.sh's post-install message includes a Q&A:

> Sandra didn't reply? Try:
> 1. `docker logs acme-cabinet | grep -i telegram` — bot polling errors
> 2. Verify `OWNER_TELEGRAM_USER_ID` in `.env` matches the actual user ID (search `@userinfobot` on Telegram)
> 3. `docker exec -it acme-cabinet bash` and check `~/.claude/channels/telegram/bot.pid` exists

**Critical for sales demos**: the "Sandra is online" message + the actual Telegram reply must happen within 15 minutes wall-clock, **including** the bot creation step (which is the operator typing in BotFather, not us). If install drags past 15 min, the prospect's DSI starts losing patience.

---

## 9. The upgrade story (missing from the north-star)

### Scenario

Client has `bubble-cabinet:v1.0` running for 6 months. Acme has accumulated ~500MB of Sandra's conversation history, ~3 active depts (Maya, Ben, Eliot), ~120 daily Restic snapshots. We release `v1.5` which:
- Adds an MCP server (e.g. weather plugin)
- Bumps Node from 22 to 24
- Adds a schema field `tenant.gate_policies.shadow_window_seconds` (DEFAULTS, so old configs are forward-compatible)
- Adds ONE breaking schema change: renames `bubble_admin_keys[]` → `external_admin_keys[]` (old depts have a fallback for one version)

### Upgrade procedure (manual, with copilot)

```
$ cd /opt/bubble-cabinet
$ ./scripts/upgrade.sh --to v1.5

[1/8] Reading current version ... v1.0 detected
[2/8] Fetching release notes for v1.5 ... see RELEASE-NOTES-v1.5.md
[3/8] Pre-upgrade Restic snapshot ... ✓ snapshot id 4f3a2b1c
[4/8] Pulling ghcr.io/bubbleinvest/bubble-cabinet:v1.5 ...
[5/8] Running migration script v1.0 -> v1.5 ... ✓
       - migrated 3 depts' bubble_admin_keys → external_admin_keys
       - added gate_policies.shadow_window_seconds defaults
[6/8] Stopping v1.0 cabinet ...
[7/8] Starting v1.5 cabinet ...
[8/8] Health check ... ✓ HEALTHY

Upgraded to v1.5 in 1m 47s. Rollback: ./scripts/upgrade.sh --rollback (uses snapshot 4f3a2b1c).
```

### Auto vs manual

**Manual only for v1.x.** Clients with data-sovereignty concerns want to schedule upgrades, not be surprised. The script exists but the operator runs it.

For v2.x (post-product-market-fit), an opt-in `auto_upgrade: minor_only` flag in `.env` could allow `docker compose pull` weekly + auto-restart, but defaults to off.

### Data migration story

Every breaking schema change ships with a `migrations/v1.x-to-v1.y.py` script that:
- Reads the volumes in read-only mode
- Writes a tmp copy with migrations applied
- Atomic-renames after dry-run validation
- Logs every change to `migrations/applied.jsonl` (audit trail)

This script runs INSIDE the new image (`docker run --rm -v cabinet-claude-home:/home/claude/.claude:rw ${NEW_IMAGE} /usr/local/bin/migrate.sh v1.0 v1.5`).

### Rollback story

`./scripts/upgrade.sh --rollback`:
1. Stop the v1.5 container.
2. Restore from the Restic snapshot taken in step 3 of the upgrade.
3. Edit `.env` to pin `CABINET_VERSION=v1.0`.
4. `docker compose up -d`.

**Rollback window**: the Restic snapshot is kept for at least 30 days (per default Restic retention). Rolling back beyond that = restore from the client's offsite backup if they have one.

### README-UPGRADE.md outline

```
# README-UPGRADE.md
1. Pre-upgrade checklist
   - Verify last Restic snapshot < 24h old
   - Read RELEASE-NOTES-vX.Y.md for breaking changes
   - Notify owner: "Sandra will be offline ~2 min"
2. Run ./scripts/upgrade.sh --to vX.Y
3. Post-upgrade validation
   - docker logs cabinet | tail -50
   - On Telegram: "Sandra, ça va ?" (should reply normally)
4. Troubleshooting
   - "Cabinet won't start after upgrade" → ./scripts/upgrade.sh --rollback
   - "Sandra says she's confused about <feature>" → check release notes for skill behavior changes
5. Annual maintenance
   - Review external_admin_keys, rotate where needed
   - Verify Restic offsite copy is working (if configured)
```

---

## 10. Per-sprint refinement

### Sprint 0 — Platform spike (NEW, ~6h)

The north-star skipped this. Without it, Sprint 1 starts blind.

- **Files**: none committed (this is a spike). Output is a 1-page memo in `prototypes/bubble-cabinet-spike/REPORT.md` answering: "Does Docker Desktop on Mac M-series mount a 5GB volume without corruption after sleep/wake? Does Podman on Windows handle our compose file? Does claude-code in a `:slim` base image actually start?"
- **Tests**: the 7 platform tests from §5 above, done by hand, results recorded.
- **Acceptance**: ≥ 5/7 platforms pass. If < 5, escalate to {{OPERATOR}} before Sprint 1.
- **Risks**: discovering on day 1 that gRPCFUSE corrupts SOPS-encrypted files (it has, historically, on certain Mac versions). Mitigation: this IS the spike's job.
- **Honest estimate**: 6h.

### Sprint 1 — Dockerfile + docker-compose + install.sh (~10h, NOT 6h)

- **Files to create**:
  - `projects/bubble-vps-platform/bubble-cabinet/Dockerfile`
  - `projects/bubble-vps-platform/bubble-cabinet/docker-compose.yml`
  - `projects/bubble-vps-platform/bubble-cabinet/.env.template`
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/install.sh`
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/setup-local-backup.sh`
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/upgrade.sh` (stub, real content in Sprint 3)
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/lib/preflight.sh`
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/lib/age-keygen.sh`
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/lib/restic-supervisor.sh`
  - `projects/bubble-vps-platform/bubble-cabinet/supervisor/cabinet-entrypoint.sh` (bash supervisor)
  - `projects/bubble-vps-platform/bubble-cabinet/supervisor/telegram-watchdog.sh`
  - `projects/bubble-vps-platform/tests/integration/test_dockerfile_build.sh`
  - `projects/bubble-vps-platform/tests/integration/test_install_preflight.sh`
  - `projects/bubble-vps-platform/lib/test_cabinet_compose.py`
- **Tests** (12, not 8):
  - `test_dockerfile_pins_versions` — verifies all version numbers in Dockerfile match SPEC.
  - `test_dockerfile_no_secrets` — greps the image for plaintext secret-like strings.
  - `test_compose_no_published_ports` — verifies no `ports:` block by default.
  - `test_compose_volumes_named` — verifies all volumes are named (no bind mounts to `/`).
  - `test_compose_resources_capped` — verifies memory/cpu limits present.
  - `test_install_preflight_fails_no_docker` — mocks `which docker` returning nothing → exit 10.
  - `test_install_preflight_fails_bad_env` — runs with incomplete .env → exit 21.
  - `test_install_idempotent` — runs install.sh twice; second run is no-op.
  - `test_supervisor_restart_on_child_crash` — kills claude-code sub-process → supervisor restarts within 30s.
  - `test_telegram_watchdog_no_token_leak` — runs watchdog with mock token, asserts token not in logs.
  - `test_age_key_mode_0400` — generates key, asserts permissions.
  - `test_image_size_under_budget` — `docker images` shows < 1.5GB compressed.
- **Acceptance criteria**:
  - Fresh Mac M3 with `.env` filled → install.sh exits 0 within 5 min.
  - Sandra replies to `/start` on Telegram within 3 min of install completion.
  - `docker compose restart cabinet` → conversation history preserved.
  - `docker compose down && docker compose up -d` → same.
- **Risks**: claude-code's auth flow inside a non-interactive container (TTY traps). Mitigation: validate auth path in Sprint 0 spike. — Bun installer scripts inside Docker layered build sometimes fail due to systemd-detect-virt false positives. Mitigation: pin Bun to a version known to work in containers, document in Dockerfile comments.
- **Honest estimate**: 10h. The north-star's 6h is optimistic by ~60%. Reason: every cross-platform test takes a full install cycle (~15 min wall-clock); doing 5 of them iteratively eats 4–6h of the sprint.

### Sprint 2 — Mode `local-git` (~6h, NOT 4h)

- **Files to modify**:
  - `projects/bubble-ops-loop/scripts/bootstrap-dept.sh` (lines 101-260: branch on `BUBBLE_GIT_PROVIDER`)
  - `projects/bubble-ops-loop/scripts/activate-dept.sh` (replace `gh pr create` path)
  - `projects/bubble-ops-loop/git-guard/src/guard.py` (line 113ish: skip broker for `file://`)
  - `projects/bubble-ops-loop/git-guard/src/staging.py` (already remote-agnostic, verify)
  - `projects/bubble-ops-loop/scripts/lib/git-provider.sh` (NEW — helper functions used by all three scripts)
- **Tests** (10, not just "extend test_qa_e2e_full_walk"):
  - `test_bootstrap_dept_local_bare_no_gh_calls` — `BUBBLE_GIT_PROVIDER=local-bare` walk asserts no `gh` invocation.
  - `test_bootstrap_dept_local_bare_creates_bare_repo` — `git init --bare` happened.
  - `test_bootstrap_dept_local_bare_remote_url_file_scheme` — `git remote -v` shows `file:///...`.
  - `test_activate_dept_local_bare_merges_via_local` — no `gh pr create`; merge commit appears on main.
  - `test_guard_push_skips_broker_for_file_remote` — broker subprocess not invoked.
  - `test_guard_push_runs_policy_for_file_remote` — path-check policy still enforced.
  - `test_guard_push_uses_broker_for_https_remote` — backward compat for VPS case.
  - `test_guard_audit_logs_provider_field` — audit entries include `provider: local-bare` or `github`.
  - `test_qa_e2e_local_bare_full_walk` — full bootstrap→step1→…→step7→activate without any network.
  - `test_qa_e2e_github_still_works` — regression: GitHub path unchanged.
- **Acceptance criteria**:
  - `BUBBLE_GIT_PROVIDER=local-bare ./bootstrap-dept.sh --slug=maya …` runs to completion offline.
  - `bubble-token-broker` binary not invoked at all during a local-bare walk.
  - All existing GitHub-path tests still pass (no regression).
- **Risks**: `bootstrap-dept.sh` has 300+ lines of GitHub-specific logic woven in; the temptation is to copy-paste into a separate branch, but that creates two divergent code paths to maintain. Mitigation: keep a single script, branch via the `BUBBLE_GIT_PROVIDER` env var, factor common steps into `lib/git-provider.sh`.
- **Honest estimate**: 6h. The git-guard change alone is ~2h (the policy/broker/push split needs careful work to not break the GitHub path).

### Sprint 3 — Doc client + upgrade procedure (~5h, NOT 2h)

- **Files to create**:
  - `projects/bubble-vps-platform/bubble-cabinet/README-INSTALL.md` (FR, 2-3 pages, the DSI-facing install guide)
  - `projects/bubble-vps-platform/bubble-cabinet/README-UPGRADE.md` (FR)
  - `projects/bubble-vps-platform/bubble-cabinet/README-DISASTER.md` (FR — on-prem-adapted DR)
  - `projects/bubble-vps-platform/bubble-cabinet/RELEASE-NOTES-v1.0.md` (template)
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/upgrade.sh` (real content this time, not the Sprint 1 stub)
  - `projects/bubble-vps-platform/bubble-cabinet/scripts/restore-from-restic.sh`
  - `projects/bubble-vps-platform/bubble-cabinet/migrations/.gitkeep` (empty; first migration ships with v1.1)
- **Tests** (5):
  - `test_upgrade_script_creates_snapshot_before_pull` — verify pre-upgrade Restic snapshot taken.
  - `test_upgrade_script_rollback_uses_latest_pre_upgrade_snapshot` — rollback restores from the correct snapshot.
  - `test_restore_from_restic_full_volume_recovery` — `down -v` then restore → Sandra resumes.
  - `test_readme_install_renders_as_valid_markdown` — markdownlint passes.
  - `test_readme_install_has_no_internal_jargon` — greps for "Lab", "Rick", "Morty", "vdk888" — none present.
- **Acceptance criteria**:
  - A DSI who does NOT know bubble-ops-loop can follow README-INSTALL.md end-to-end without calling us.
  - upgrade.sh + rollback.sh are exercised in a manual test (no real v1.1 yet — uses a dummy `v1.0.1` tag for the round-trip).
  - All FR docs reviewed by a French native speaker for tone (Bureau-de-Cadre voice).
- **Risks**: doc quality is a "you know it when you see it" thing — {{OPERATOR}} should be the reviewer on Sprint 3 acceptance. Mitigation: send drafts via Telegram for sign-off.
- **Honest estimate**: 5h. The north-star's 2h treats docs as an afterthought; that's how bad docs ship. 2h is "first draft of one of the three READMEs".

**Sprint totals: 0 + 1 + 2 + 3 = 6 + 10 + 6 + 5 = 27 hours.** Roughly **2.25x the north-star's 12h estimate.** This is consistent with the "first-real-product factor" empirically observed on the VPS platform (`bubble-vps-platform` itself shipped at ~2x its initial estimate).

---

## 11. Deal-breaker list

If any of these turns out to be true, Bubble Cabinet is not sellable in its current form. Each needs a Sprint 0 validation OR a clear contingency.

| # | Deal-breaker | How to detect | Mitigation if it fires |
|---|---|---|---|
| 1 | **Docker Desktop on Mac M-series corrupts named volumes after sleep/wake** | T-SLEEP-1 in Sprint 0 spike. | Force-document Mac as "dev only, not for production demos". Push Linux VPS hosting Docker as the on-prem path. |
| 2 | **Claude Code's interactive auth flow can't complete inside a non-interactive container** (e.g. the OAuth callback wants a browser) | Sprint 0 spike: validate both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` paths in a fresh container. | If OAuth path is broken, ship API-key-only for v1.0; ship OAuth in v1.1 once we figure out the headless flow (probably involves storing the token externally and mounting it). |
| 3 | **Most prospects refuse to install Docker Desktop due to licensing** | Sales conversation. We won't know until we pitch. | Podman Desktop fallback (already planned). Have a one-pager comparing the three engines ready for the DSI. |
| 4 | **Restic backup of `cabinet-claude-home` while Sandra is actively writing causes corruption** (the agent-memory + transcript files are constantly mutating) | Manual test: 1h of conversation while restic runs every 5 min, then restore from the middle of a conversation and check for consistency. | Use Restic's `--exclude` for the JSONL transcript files (they're append-only; partial-snapshot OK). For agent-memory, switch to LVM snapshots on Linux hosts OR accept "atomic snapshot of last completed turn". |
| 5 | **The bash supervisor leaks child processes** (claude crashes, watchdog crashes, but the container stays "healthy") | Stress test: kill -9 various sub-processes randomly over 24h, count zombie/orphan processes. | If supervisor proves fragile, fall back to s6-overlay. ~3h to swap. |

The deal-breaker that scares me most is **#4** (Restic + live agent-memory). Both VPS and Cabinet share this risk, but on VPS we have systemd hooks to quiesce before backup; in a container the orchestration is harder. **Recommend a Sprint 0.5 spike on this specifically.**

---

## 12. T-a-a-S comparison

The sister roadmap `ROADMAP-TENANT-AS-A-SERVICE.md` lists 5 sprints (~14h) for the cloud version. Map of overlap:

| T-a-a-S sprint | Bubble Cabinet equivalent | Shared work? |
|---|---|---|
| 1. Persona templating (parameterize morty into a reusable concierge template) | Concierge "Sandra" template generation in install.sh's step 4 | **100% shared** — both need exactly the same envsubst-on-template mechanism. Build once. |
| 2. bubble-ops-loop as deployable dependency | bubble-ops-loop baked into the cabinet image | **~80% shared** — the packaging mechanism differs (VPS = rsync from data repo at deploy time; Cabinet = embedded at image-build time), but the *contents* are identical. |
| 3. Concierge skills + gate policies (`dept-supervisor`, `restart_stuck_dept`, etc.) | Same skills for Sandra | **100% shared** — Sandra and the VPS concierge run the same skill set. |
| 4. Bubble remote access (`bubble_admin_keys[]` → `/root/.ssh/authorized_keys`) | NOT applicable to on-prem (the client doesn't want our SSH key on their box by default) | **0% shared**, opposite direction even. Cabinet's analogue is: "client invites Bubble Invest via Tailscale share or WireGuard, on demand, not by default". |
| 5. End-to-end "fresh tenant Marie" integration test | "Fresh tenant Acme on a Mac" integration test | **~70% shared** — the test rig is similar (spin up fresh env, run install, assert Sandra answers `/start`), but the substrate differs (hcloud VM for T-a-a-S, Docker Desktop for Cabinet). |

**Sequencing recommendation for Rick**: do T-a-a-S Sprints 1 + 2 + 3 FIRST (they're shared). Then split: T-a-a-S Sprints 4 + 5 in one track, Bubble Cabinet Sprints 0 + 1 + 2 + 3 in a parallel track. The Cabinet track can start as soon as T-a-a-S Sprint 1's persona template is shaped.

Total non-shared work (Cabinet-specific):
- Sprint 0 spike: 6h
- Sprint 1 Dockerfile/compose/install.sh (less the persona template, which is shared): ~8h
- Sprint 2 local-git mode: 6h (fully Cabinet-specific; doesn't help T-a-a-S)
- Sprint 3 docs: 5h
- = **25h Cabinet-only** on top of the shared ~10h of T-a-a-S Sprints 1-3.

**Recommendation**: pitch {{OPERATOR}} on "Bubble Cabinet v1.0 ships ~4 weeks after T-a-a-S Sprint 3 wraps", not "12h total".

---

## Closing

Two artifacts to treat as standalone: the **VPS-to-Container task map** (§6) tells the Sprint 1 sub-agent which pyinfra tasks to reuse vs ignore; the **Deal-breaker list** (§11) is what Sprint 0 must validate first.

Questions for {{OPERATOR}} before kickoff: (a) Debian vs Ubuntu base, (b) Podman as officially-supported alternative, (c) the new Sprint 0 spike, (d) the 12h → 27h estimate revision.
