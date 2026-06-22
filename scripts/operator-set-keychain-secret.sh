#!/usr/bin/env bash
# =============================================================================
# operator-set-keychain-secret.sh — auth skill Flow 3 (Keychain primitive).
#
# Stocke/lit/supprime un secret dans le macOS Keychain natif. Symétrique de
# operator-set-secret.sh (Flow 2) qui cible SOPS. Cas d'usage canonique :
# passphrases qui PROTÈGENT SOPS et ne peuvent donc pas vivre dedans
# (boucle de bootstrap).
#
# Exemple : la passphrase qui chiffre le backup de /etc/age/key.txt. Si on
# la stocke chiffrée avec la clé age, et qu'on perd la clé age, on ne peut
# plus déchiffrer la passphrase pour récupérer la clé. Le Keychain natif
# macOS rompt cette boucle — il est indépendant de la chaîne SOPS.
#
# Created: 2026-05-21, {{OPERATOR}} directive msg 2823-2825.
# =============================================================================
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage : operator-set-keychain-secret.sh --mode=<set|get|delete> \
                                          --service=<name> \
                                          --account=<name> \
                                          [--label="..."]

Modes (3) :
  --mode=set     stocker un nouveau secret OU mettre à jour si existe.
                 Ouvre un prompt osascript GUI (saisie masquée). Idempotent
                 via le flag -U de `security`.
  --mode=get     lire silencieusement un secret depuis le Keychain et
                 l'écrire sur stdout (single line, no metadata). Sortie
                 prévue pour `PASS=$(...)`. Exit 1 si non trouvé.
  --mode=delete  supprimer un secret existant (pour rotation/réinitialisation).
                 Exit 0 même si déjà absent (idempotent).

Arguments requis :
  --service=<name>    identifiant du service (ex: "bubble-age-backup")
  --account=<name>    identifiant du compte (ex: "morty")
                      Tous deux : alphanumériques + '-' '_' '.' uniquement.

Optionnel :
  --label="texte"     texte affiché dans le prompt osascript (mode set
                      uniquement). Défaut : nom du service.
  --help              afficher cette aide

Cas d'usage canonique — passphrase backup age-key Morty :
  Le backup de /etc/age/key.txt (cf. scripts/backup-age-key.sh) doit être
  chiffré, mais la passphrase ne peut PAS vivre dans la chaîne SOPS
  (bootstrap cycle). Donc on la stocke dans le Keychain macOS :

  # Première fois (stocke depuis un GUI prompt) :
  bubble-set-keychain --mode=set --service=bubble-age-backup --account=morty \
      --label="Passphrase pour le backup de la clé age Morty"

  # Backups suivants (lecture silencieuse depuis Keychain) :
  PASSPHRASE=$(bubble-get-keychain --mode=get --service=bubble-age-backup --account=morty)

  # Rotation (supprime puis re-set) :
  bubble-set-keychain --mode=delete --service=bubble-age-backup --account=morty

Symétrie avec Flow 2 (`bubble-set-secret`) :
  bubble-set-secret   → cible SOPS (.sops.env)            → secrets API, tokens
  bubble-set-keychain → cible Keychain macOS (native)     → passphrases qui
                                                            protègent SOPS

Sécurité :
  - Mode SET : prompt osascript avec "hidden answer" (saisie masquée, pas
    d'écho, pas de scrollback terminal, pas d'historique shell).
  - Mode GET : sortie stdout uniquement, jamais loggée ni envoyée ailleurs.
  - Aucun secret n'est écrit en clair sur disque.
  - Aucun secret n'apparaît dans les transcripts JSONL des sessions Claude.
USAGE
}

# -------- early exit conditions -------

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

# Quick --help check before stricter parsing
for arg in "$@"; do
  if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
    usage
    exit 0
  fi
done

# -------- platform guard -------
# This script is macOS-only (uses /usr/bin/security + osascript).
if ! command -v security >/dev/null 2>&1; then
  echo "ERROR: /usr/bin/security not found. This script requires macOS." >&2
  exit 3
fi
if ! command -v osascript >/dev/null 2>&1; then
  echo "ERROR: osascript not found. This script requires macOS." >&2
  exit 3
fi

# -------- arg parsing -------

MODE=""
SERVICE=""
ACCOUNT=""
LABEL=""

for arg in "$@"; do
  case "$arg" in
    --mode=*)    MODE="${arg#*=}" ;;
    --service=*) SERVICE="${arg#*=}" ;;
    --account=*) ACCOUNT="${arg#*=}" ;;
    --label=*)   LABEL="${arg#*=}" ;;
    --help|-h)   usage ; exit 0 ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# -------- validate mode -------
case "$MODE" in
  set|get|delete) ;;
  "")
    echo "ERROR: --mode is required. Valid: set, get, delete" >&2
    exit 2
    ;;
  *)
    echo "ERROR: invalid mode '$MODE'. Valid: set, get, delete" >&2
    exit 2
    ;;
esac

# -------- validate required args -------
if [[ -z "$SERVICE" ]]; then
  echo "ERROR: --service is required" >&2
  exit 2
fi
if [[ -z "$ACCOUNT" ]]; then
  echo "ERROR: --account is required" >&2
  exit 2
fi

# -------- safety: validate service + account names -------
# Only allow alphanumerics + - _ . to prevent shell metacharacter injection.
_SAFE_PATTERN='^[A-Za-z0-9._-]+$'
if ! [[ "$SERVICE" =~ $_SAFE_PATTERN ]]; then
  echo "ERROR: --service contains unsafe characters. Allowed: [A-Za-z0-9._-]" >&2
  exit 2
fi
if ! [[ "$ACCOUNT" =~ $_SAFE_PATTERN ]]; then
  echo "ERROR: --account contains unsafe characters. Allowed: [A-Za-z0-9._-]" >&2
  exit 2
fi

# Default label = service name
if [[ -z "$LABEL" ]]; then
  LABEL="Bubble secret: $SERVICE / $ACCOUNT"
fi

# -------- mode dispatch -------

case "$MODE" in

  set)
    # Open osascript GUI prompt for the value. Hidden answer = masked input.
    PROMPT="$LABEL"
    TITLE="Bubble — Stocker un secret dans le Keychain"
    # 2>/dev/null suppresses "execution error" noise when user cancels.
    value=$(/usr/bin/osascript <<OSA 2>/dev/null
      try
          set result to text returned of (display dialog "$PROMPT" ¬
              with title "$TITLE" ¬
              default answer "" ¬
              with hidden answer ¬
              buttons {"Annuler", "Stocker"} ¬
              default button "Stocker")
          return result
      on error
          return "USER_CANCELLED"
      end try
OSA
)
    if [[ "$value" == "USER_CANCELLED" || -z "$value" ]]; then
      echo "Annulé (rien de stocké)." >&2
      exit 1
    fi

    # Pipe the value to security via stdin (-w flag value comes from -p stdin).
    # Use printf '%s' to avoid trailing newline that would land in the keychain.
    # -U = update if exists (idempotent), -s service, -a account, -w password from stdin via -p.
    # NOTE: security takes the password as -w VALUE, NOT from stdin. Use -w "$value".
    # This is the canonical pattern for `add-generic-password`.
    if /usr/bin/security add-generic-password \
         -U \
         -s "$SERVICE" \
         -a "$ACCOUNT" \
         -w "$value" \
         -l "$LABEL" 2>/dev/null; then
      echo "OK — secret stocké dans le Keychain (service=$SERVICE, account=$ACCOUNT)"
      # Wipe the local variable (best-effort; bash can't truly wipe memory)
      value=""
      exit 0
    else
      echo "ERROR: security add-generic-password failed" >&2
      value=""
      exit 4
    fi
    ;;

  get)
    # Print the password ONLY (no metadata) to stdout. -w is the magic flag.
    # If not found, security exits 44 with a message on stderr.
    if pw=$(/usr/bin/security find-generic-password \
              -s "$SERVICE" \
              -a "$ACCOUNT" \
              -w 2>/dev/null); then
      printf '%s' "$pw"
      exit 0
    else
      echo "ERROR: secret not found in Keychain (service=$SERVICE, account=$ACCOUNT)" >&2
      exit 1
    fi
    ;;

  delete)
    # Idempotent: exit 0 even if already absent.
    if /usr/bin/security delete-generic-password \
         -s "$SERVICE" \
         -a "$ACCOUNT" >/dev/null 2>&1; then
      echo "OK — secret supprimé du Keychain (service=$SERVICE, account=$ACCOUNT)"
    else
      echo "Note : secret déjà absent du Keychain (idempotent OK)"
    fi
    exit 0
    ;;

esac
