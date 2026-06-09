#!/usr/bin/env bash
# bubble-vps-platform — friendly wrapper around pyinfra inventory.py deploy.py.
#
# Usage:
#   ./scripts/deploy.sh --tenant=bubble-internal [extra-pyinfra-args...]
#   ./scripts/deploy.sh --tenants=all [extra-pyinfra-args...]
#
# Examples:
#   ./scripts/deploy.sh --tenant=bubble-internal
#   ./scripts/deploy.sh --tenant=bubble-internal --dry-run
#   ./scripts/deploy.sh --tenants=all --limit linux_hosts

set -euo pipefail

TENANT_ARG=""
ALL_ARG=""
EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --tenant=*)
            TENANT_ARG="${arg#--tenant=}"
            ;;
        --tenants=all)
            ALL_ARG="1"
            ;;
        --tenants=*)
            echo "error: --tenants= only accepts the literal value 'all' (got: ${arg#--tenants=}). Use --tenant=<name> for a single tenant." >&2
            exit 2
            ;;
        *)
            EXTRA_ARGS+=("$arg")
            ;;
    esac
done

if [[ -z "$TENANT_ARG" && -z "$ALL_ARG" ]]; then
    echo "error: must specify --tenant=<name> OR --tenants=all" >&2
    echo "  ./scripts/deploy.sh --tenant=bubble-internal" >&2
    echo "  ./scripts/deploy.sh --tenants=all" >&2
    exit 2
fi

if [[ -n "$TENANT_ARG" && -n "$ALL_ARG" ]]; then
    echo "error: --tenant= and --tenants=all are mutually exclusive" >&2
    exit 2
fi

# Repo root = parent of this script's directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "$TENANT_ARG" ]]; then
    export TENANT="$TENANT_ARG"
    unset TENANTS_ALL
else
    export TENANTS_ALL="1"
    unset TENANT
fi

# SOPS_AGE_KEY_FILE — sops needs to know where the operator's age private key
# lives. The bootstrap script puts it at ~/.config/sops/age/keys.txt. Export
# here so deploy.sh works regardless of whether the operator's shell rc has
# it. Operator-supplied value (if already set) wins.
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"

# Default flags per SPEC-004 + Step 2:
#   --retry 2 + --retry-delay 5: handles transient SSH drops (UFW rate-limit
#     is 6 conn/30s; this gives backoff window for the limit to clear).
#   --sudo: tenant.yaml's host.ssh_user is a non-root account (e.g. `claude`)
#     with NOPASSWD sudo; pyinfra needs --sudo to escalate for apt, file writes
#     in /etc, systemd, etc.
#   -y: skip the interactive "Press enter to execute" prompt — deploy.sh is
#     used both interactively and from CI / scripts, and the operator already
#     made the choice to run.
# Operator can override by re-passing their own --retry/--retry-delay.
DEFAULT_FLAGS=("--retry" "2" "--retry-delay" "5" "--sudo" "-y")

exec pyinfra "${DEFAULT_FLAGS[@]}" inventory.py deploy.py "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
