# North Star — Bubble Cabinet (le produit packagé)

**Audience**: tout sub-agent qui prend la mission "Bubble Local" du R&D BACKLOG.md. **Lis ce doc AVANT de coder une seule ligne.** Il décrit l'expérience opérateur cible — la chose qu'on essaie de rendre vraie. Sans cette boussole, tu vas faire des choix techniquement corrects mais qui ratent la vision.

**Source conversation**: {{OPERATOR}} ↔ Rick Telegram msgs 2799-2816, 2026-05-21 après-midi.
**Status**: vision validée par {{OPERATOR}}. Implémentation à venir (12h estimées sur 3 sprints).

---

## Le produit en une phrase

**Bubble Cabinet** est un cabinet d'éclosion agentique **packagé en Docker container**, livrable sur clé USB ou par téléchargement, qui s'installe en ~1h chez un client sur son propre serveur Linux (ou Mac/Windows via Docker Desktop) et lui donne **un concierge agent personnalisé** + le **framework bubble-ops-loop** pour qu'il éclôse ses propres collègues agentiques (Maya, Ben, etc.) — **zéro donnée ne sort de chez lui**.

C'est l'offre miroir de "Tenant-as-a-Service" (qui est cloud Hetzner/OVH) — même framework, packaging différent, cible client différente (banques, juridique, médical, gov, et tout prospect data-sovereignty-sensitive).

---

## Pourquoi ça s'appelle Bubble Cabinet

Cohérent avec tout le vocabulaire Bureau-de-Cadre déjà installé :
- "Cabinet d'éclosion" sur la console (UX redesign 2026-05-20)
- "Collègue éclos / Cérémonie d'arrivée / Concierge"
- Le client achète littéralement "son cabinet Bubble"

**NE PAS** confondre avec :
- `bubble-ops-loop` = le **framework** (le code des 7 étapes, les runners, les testers). C'est UN composant de Bubble Cabinet.
- `bubble-vps-platform` = le **provisioneur** pour le cas cloud. C'est l'analogue cloud de Bubble Cabinet.
- Bubble Cabinet emballe `bubble-ops-loop` + un concierge + un git local + des backups, le tout en 1 container.

---

## Le process opérateur cible (l'expérience à rendre vraie)

C'est le scénario qui DOIT marcher après l'implémentation des 3 sprints. Tout choix technique doit servir cette expérience.

### Contexte du RDV

Rick (toi en tant que sub-agent, ou un humain Bubble Invest dans la vraie vie) arrive chez Acme Corp. Le DSI dit "OK installez-nous votre truc sur notre serveur Ubuntu". Tu as :
- Ton ordi
- Une clé USB (ou un téléchargement depuis github.com/bubbleinvest/bubble-cabinet)
- ~1h devant toi

Le client a :
- 1 serveur Ubuntu/Debian (4 vCPU / 8 Go RAM / 50 Go disque minimum)
- Docker Engine + docker-compose-plugin installés
- 1 compte Telegram personnel pour le owner désigné (souvent le PDG ou un opérationnel proche)
- 1 compte Anthropic (API key OU subscription Claude Code)

### Le process en 6 étapes

**Étape 1 — Tu débranches ta clé USB ou tu télécharges**
- Contenu : `bubble-cabinet/` autosuffisant
  - `docker-compose.yml`
  - `Dockerfile` (ou pointe vers `bubbleinvest/bubble-cabinet:vX.Y` sur Docker Hub si on pre-build)
  - `.env.template` — à remplir avec le client
  - `scripts/install.sh` — wrapper one-shot
  - `scripts/setup-local-backup.sh`
  - `README-INSTALL.md` — en français, opérateur-friendly (= leur DSI qui ne connaît PAS notre framework)
  - `docs/` — DR playbook + backup strategy adaptés au contexte on-prem

**Étape 2 — Créer le bot Telegram avec le client** (~5 min)
- Ouvrir Telegram sur l'écran du client
- Aller chez @BotFather
- `/newbot`
- Nom : "Sandra Acme" (le concierge nommé par le client) / handle `bubblecabinet_acme_bot`
- Récupérer le token
- Le coller dans `.env`

**Étape 3 — Transférer le dossier sur leur serveur**
- `scp` depuis ton ordi, ou copie depuis la clé USB
- Destination : `/opt/bubble-cabinet/` (ou ce que leur DSI préfère)

**Étape 4 — Lancer `./scripts/install.sh`** sur leur serveur
Le script doit :
1. Vérifier les prérequis (docker présent, .env rempli)
2. Build le container OU pull l'image
3. Initialiser les SOPS+age keys du tenant (génère la clé age locale, mode 400, dans un volume Docker)
4. `docker-compose up -d`
5. Attendre que le concierge soit healthy
6. Afficher en clair : "Sandra est en ligne. Ouvre Telegram, cherche @bubblecabinet_acme_bot, tape /start"

**Étape 5 — Le client tape `/start` et Sandra se présente**
- Sandra : "Bonjour [Owner]. Je suis Sandra, votre concierge Bubble Cabinet. Je peux vous aider à : (1) accueillir un premier collègue agentique (Maya pour prospection, Ben pour finance, etc.), (2) surveiller ce qui se passe sur votre serveur, (3) vous expliquer comment ça marche. Par quoi on commence ?"
- Sandra parle uniquement au owner désigné (Telegram allowlist par ID)

**Étape 6 — Premier collègue éclos** (souvent un 2e RDV)
- Le client : "Je veux Maya pour ma prospection LinkedIn"
- Sandra propose : "Très bien. On va passer par 7 étapes ensemble (~30 min). Tu prêt ?"
- Éclosion bubble-ops-loop standard via Sandra

**Étape 7 (optionnel à la mise en route) — Backup local + DR docs**
- Run `./scripts/setup-local-backup.sh`
- Configure Restic vers un autre disque local du client (NAS interne, autre serveur, etc.)
- Imprime/email `docs/DISASTER-RECOVERY.md` au DSI

### Total : ~1h de RDV chez le client

Tu repars, il a son cabinet qui tourne, son concierge Sandra qui parle au owner sur Telegram. Pas de cloud, pas de fuite, sa data reste sur ses serveurs.

---

## Décisions architecturales validées par {{OPERATOR}}

1. **Garde Telegram** comme canal de communication (msg 2807) — pas d'effort sur Matrix/Signal alternative
2. **Un concierge par tenant, customizable** — le client choisit le nom (Sandra, Karl, etc.) et idéalement la voix/style
3. **Le concierge talks to owner only** — pas de cross-visibility
4. **Le concierge peut créer/modifier/supprimer des collègues éclos** avec gate policies pour pas faire de bêtises
5. **Bubble (Bubble Invest) a accès distant si le client l'autorise** — bloc `bubble_admin_keys[]` dans la config tenant, géré via SSH key déposée dans authorized_keys
6. **Zero outbound network sauf api.anthropic.com** (et Telegram api.telegram.org bien sûr) — Docker network policies + firewall hôte

---

## Sprints d'implémentation (les 3 que les sub-agents vont prendre)

### Sprint 1 — Dockerfile + docker-compose + install.sh (~6h)

**Goal** : produire un dossier `bubble-cabinet/` qu'on peut zipper sur USB.

Sous-livrables :
- `bubble-cabinet/Dockerfile` Ubuntu 24.04 base avec :
  - `apt install` : git, curl, openssh-client, sops, age, python3.12, claude-code (via bun), restic, jq, yq, gettext
  - `npm i -g @anthropic-ai/claude-code` ou install bun + claude
  - User `claude` non-root + sudoers minimal
  - `WORKDIR /home/claude`
  - `ENTRYPOINT` = `script -qfc + claude --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official`
- `bubble-cabinet/docker-compose.yml` :
  - Service `cabinet` : build context = . OR image = bubbleinvest/bubble-cabinet:vX.Y
  - Volumes persistents : `cabinet-data:/home/claude/.claude`, `cabinet-age:/etc/age`, `cabinet-secrets:/etc/bubble`, `cabinet-git:/srv/git-local`, `cabinet-backups:/var/backups/bubble-restic`
  - Network isolé : `network_mode: bridge` + `extra_hosts` que pour api.anthropic.com et api.telegram.org
  - EnvironmentFile = .env
- `bubble-cabinet/.env.template` :
  - `OWNER_DISPLAY_NAME=` (ex: "Marie Dupont")
  - `OWNER_TELEGRAM_USER_ID=` (le client tape /start au bot puis on regarde l'event ID)
  - `CONCIERGE_NAME=` (ex: "Sandra")
  - `TENANT_NAME=` (ex: "acme")
  - `TELEGRAM_BOT_TOKEN=` (récupéré chez @BotFather étape 2)
  - `ANTHROPIC_API_KEY=` OU `CLAUDE_SUBSCRIPTION_AUTH_MODE=1`
- `bubble-cabinet/scripts/install.sh` :
  - check_prereqs (docker, docker-compose, .env rempli)
  - generate age key si pas déjà fait (mode 400 dans volume cabinet-age)
  - generate Restic passphrase si pas déjà fait
  - `docker compose build && docker compose up -d`
  - attendre healthy
  - afficher message "Sandra est en ligne. Telegram @bot_handle, tape /start"

TDD : 8+ tests qui mockent Docker + vérifient le contenu du Dockerfile/compose/scripts.

### Sprint 2 — Mode `local-git` (~4h)

**Goal** : que `bootstrap-dept.sh` / `git-guard` / `activate-dept.sh` marchent SANS GitHub, avec un git-bare local.

Sous-livrables :
- Variable `BUBBLE_GIT_PROVIDER=local-bare | github` lue par bootstrap-dept.sh
- Si `local-bare` :
  - `git init --bare /srv/git-local/bubble-ops-<slug>.git` au lieu de `gh repo create`
  - Pas de `gh repo view` (skip)
  - Pas de `gh pr create` (skip — activation = direct merge sur main du local-bare via une PR-équivalente locale qui consiste en un merge commit déclaratif)
- `git-guard` détecte si remote est `file:///srv/git-local/...` → skip le mint de token broker (pas nécessaire — c'est local fs)
- Tests : un walk complet bootstrap → activate sans aucun appel `gh` ni `https://github.com`

TDD : extension du `test_qa_e2e_full_walk` pour le mode local-bare.

### Sprint 3 — Doc client + procédure d'upgrade (~2h)

**Goal** : un DSI client peut installer + upgrader sans nous appeler.

Sous-livrables :
- `bubble-cabinet/README-INSTALL.md` (français, 2 pages max) :
  - Prérequis
  - 6 étapes du process opérateur ci-dessus, version client-facing
  - Troubleshooting basique (docker logs, restart container)
- `bubble-cabinet/README-UPGRADE.md` :
  - Comment passer d'une version Bubble Cabinet à la suivante sans perdre la data
  - `docker compose pull && docker compose down && docker compose up -d`
  - Backup restic avant upgrade obligatoire
- `bubble-cabinet/README-DISASTER.md` :
  - Versionné de `docs/DISASTER-RECOVERY.md` adapté on-prem

---

## Anti-patterns à ÉVITER (les sub-agents lisent ça)

1. **Ne PAS** réinventer ce qui existe dans bubble-ops-loop. Le framework existe, tu le COPIES dans le container, tu ne le réécris pas.
2. **Ne PAS** hardcoder "{{OPERATOR}}" / "Rick" / "Morty" / "bubble-internal" dans les templates. Tout doit être paramétrable via .env.
3. **Ne PAS** ajouter de dépendance cloud (S3, Sentry, Datadog, Slack, etc.). Le client a choisi local pour une raison.
4. **Ne PAS** scope-creep vers "et aussi Gitea, et aussi Prometheus, et aussi…". MVP = juste ce qu'il faut pour que le scénario de RDV ci-dessus marche.
5. **Ne PAS** essayer de tout faire en 1 sprint. 3 sprints séquentiels, chacun shippable et testable. Le client peut acheter Sprint 1+3 sans Sprint 2 si on n'a pas eu le temps (mode "github-mais-en-mode-privé-vdk888" temporaire).
6. **Ne PAS** négliger la doc opérateur — le client client-final ne va pas lire notre code, il va lire les 3 READMEs. S'ils sont mauvais, on perd le client à la première intervention.

---

## Test final d'acceptation

Tu peux déclarer "Bubble Cabinet shippable" si et seulement si :

✅ Sur un Mac fresh, tu télécharges le dossier `bubble-cabinet/`, tu remplis le `.env`, tu run `./scripts/install.sh`, et **15 min plus tard** un concierge Sandra te parle sur Telegram et te propose les 3 actions de démarrage.

✅ Tu réussis à éclore Maya via Sandra **sans toucher au shell** (tout via Telegram + console web exposée sur localhost).

✅ Tu peux faire `docker compose down && docker compose up -d` et Sandra reprend sa conversation là où elle s'était arrêtée (persistance OK).

✅ Tu peux `docker compose down -v` puis restaurer depuis Restic et tout repart (DR OK).

✅ Un DSI moyennement technique (qui ne connait PAS bubble-ops-loop) peut suivre le README-INSTALL.md sans nous appeler.

Si UN SEUL de ces 5 critères n'est pas atteint, c'est pas shippable, retour aux ateliers.

---

## Quand demander à {{OPERATOR}} avant de poursuivre

Si en cours d'implémentation tu rencontres :
- Une décision architecturale non couverte par ce doc (ex: "Gitea sidecar ou pas ?" — {{OPERATOR}} doit trancher, défaut = git-bare sans UI)
- Un trade-off coût/qualité non évident
- Un test d'acceptation qui semble impossible à atteindre
- Une dépendance externe qu'on n'avait pas anticipée

→ **Stop. Pose la question à {{OPERATOR}} via Rick** (l'orchestrateur). Ne devine pas, ne scope-creep pas. Le coût d'un message Telegram = 30 secondes. Le coût d'une mauvaise décision archi = des heures de refacto.

---

**Dernière chose** : ce doc est la **boussole**, pas la **carte**. Il décrit où on va, pas le détail de chaque ligne de code. Les sub-agents sont autonomes sur le COMMENT, mais doivent référer ce doc pour le POURQUOI et le QUOI.
