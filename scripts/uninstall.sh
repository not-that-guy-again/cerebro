#!/usr/bin/env bash
# Uninstaller for Cerebro.
#
# Removes the symlink at ~/.local/bin/cerebro, the source clone at
# ~/.local/share/cerebro, and (with explicit confirmation) the state
# directory at ~/.config/cerebro.
#
# Usage:
#   bash scripts/uninstall.sh
#   curl -fsSL <stable-url>/uninstall.sh | bash
#
# Environment:
#   CEREBRO_ASSUME_YES=1      Answer "yes" to every confirmation prompt,
#                             including state-directory removal.
#   CEREBRO_NONINTERACTIVE=1  Refuse to prompt; abort if a prompt is needed.

set -euo pipefail

CLONE_DIR="$HOME/.local/share/cerebro"
STATE_DIR="$HOME/.config/cerebro"
BIN_DIR="$HOME/.local/bin"
SYMLINK_PATH="$BIN_DIR/cerebro"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_RESET=$'\033[0m'
else
    C_BOLD=""
    C_DIM=""
    C_RED=""
    C_GREEN=""
    C_YELLOW=""
    C_BLUE=""
    C_RESET=""
fi

# Mark these as exported so shellcheck does not flag them as unused; they're
# kept in the same shape as install.sh for visual symmetry.
: "${C_BOLD}${C_RED}${C_YELLOW}"

info()    { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
step()    { printf '%s -%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
success() { printf '%s==>%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()    { printf '%s!!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()     { printf '%sxx%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }

abort() {
    err "$*"
    exit 1
}

confirm() {
    local prompt="$1"
    local default_answer="${2:-no}"
    local default_hint reply

    if [ "${CEREBRO_ASSUME_YES:-0}" = "1" ]; then
        return 0
    fi
    if [ "${CEREBRO_NONINTERACTIVE:-0}" = "1" ]; then
        abort "non-interactive mode: cannot prompt: $prompt"
    fi

    if [ "$default_answer" = "yes" ]; then
        default_hint="[Y/n]"
    else
        default_hint="[y/N]"
    fi

    while true; do
        if [ -t 0 ]; then
            printf '%s %s ' "$prompt" "$default_hint"
            read -r reply || reply=""
        elif [ -r /dev/tty ]; then
            printf '%s %s ' "$prompt" "$default_hint" >/dev/tty
            read -r reply </dev/tty || reply=""
        else
            abort "no terminal available for prompt; re-run with CEREBRO_ASSUME_YES=1 or CEREBRO_NONINTERACTIVE=1: $prompt"
        fi

        if [ -z "$reply" ]; then
            reply="$default_answer"
        fi

        case "$reply" in
            y|Y|yes|YES|Yes) return 0 ;;
            n|N|no|NO|No)    return 1 ;;
            *) printf 'please answer y or n.\n' ;;
        esac
    done
}

remove_symlink() {
    if [ -L "$SYMLINK_PATH" ]; then
        rm -f "$SYMLINK_PATH"
        step "removed symlink $SYMLINK_PATH"
    elif [ -e "$SYMLINK_PATH" ]; then
        warn "$SYMLINK_PATH exists but is not a symlink; leaving it alone."
    else
        step "no symlink at $SYMLINK_PATH"
    fi
}

remove_clone() {
    if [ -d "$CLONE_DIR/.git" ]; then
        rm -rf "$CLONE_DIR"
        step "removed source clone $CLONE_DIR"
    elif [ -e "$CLONE_DIR" ]; then
        warn "$CLONE_DIR exists but is not a git checkout; leaving it alone."
    else
        step "no source clone at $CLONE_DIR"
    fi
}

remove_state() {
    if [ ! -e "$STATE_DIR" ]; then
        step "no state directory at $STATE_DIR"
        return 0
    fi
    warn "$STATE_DIR contains your venv, logs, and plugin state."
    if confirm "Delete $STATE_DIR? This is destructive." "no"; then
        rm -rf "$STATE_DIR"
        step "removed state directory $STATE_DIR"
    else
        step "leaving $STATE_DIR in place"
    fi
}

main() {
    info "Cerebro uninstaller"
    remove_symlink
    remove_clone
    remove_state
    success "uninstall complete."
}

main "$@"
