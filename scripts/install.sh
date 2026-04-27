#!/usr/bin/env bash
# Bootstrap installer for Cerebro.
#
# Brings a fresh macOS or Linux machine to a state where `cerebro` is on PATH,
# pinned to its own venv at ~/.config/cerebro/venv, with the source clone at
# ~/.local/share/cerebro and a shim at ~/.local/bin/cerebro.
#
# Re-running on an already-installed system is safe: existing state is
# detected and the user is offered to update rather than clobber.
#
# Usage:
#   curl -fsSL <stable-url>/install.sh | bash
#   bash scripts/install.sh
#
# Environment:
#   CEREBRO_REPO_URL          Override the git URL to clone from.
#   CEREBRO_REPO_REF          Optional ref (branch/tag/sha) to check out.
#   CEREBRO_ASSUME_YES=1      Answer "yes" to every confirmation prompt.
#   CEREBRO_NONINTERACTIVE=1  Refuse to prompt; abort if a prompt is needed.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

CEREBRO_REPO_URL="${CEREBRO_REPO_URL:-https://github.com/not-that-guy-again/cerebro.git}"
CEREBRO_REPO_REF="${CEREBRO_REPO_REF:-}"

CLONE_DIR="$HOME/.local/share/cerebro"
STATE_DIR="$HOME/.config/cerebro"
VENV_DIR="$STATE_DIR/venv"
SHIM_DIR="$STATE_DIR/bin"
SHIM_PATH="$SHIM_DIR/cerebro"
BIN_DIR="$HOME/.local/bin"
SYMLINK_PATH="$BIN_DIR/cerebro"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

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

info()    { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
step()    { printf '%s -%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
success() { printf '%s==>%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()    { printf '%s!!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()     { printf '%sxx%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }

abort() {
    err "$*"
    exit 1
}

# ---------------------------------------------------------------------------
# Confirmation prompts
#
# The script may be piped from curl, in which case stdin is not a TTY. We
# read from /dev/tty when stdin is unavailable, and respect
# CEREBRO_ASSUME_YES / CEREBRO_NONINTERACTIVE for fully unattended runs.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

PLATFORM=""        # one of: macos, linux-apt, linux-pacman
PKG_MANAGER=""     # brew, apt, pacman

detect_platform() {
    local uname_s
    uname_s="$(uname -s)"
    case "$uname_s" in
        Darwin)
            PLATFORM="macos"
            PKG_MANAGER="brew"
            ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                PLATFORM="linux-apt"
                PKG_MANAGER="apt"
            elif command -v pacman >/dev/null 2>&1; then
                PLATFORM="linux-pacman"
                PKG_MANAGER="pacman"
            else
                abort "unsupported Linux distribution: need apt or pacman"
            fi
            ;;
        *)
            abort "unsupported platform: $uname_s (Cerebro supports macOS and Linux only)"
            ;;
    esac
    info "platform: $PLATFORM (package manager: $PKG_MANAGER)"
}

# ---------------------------------------------------------------------------
# Homebrew (macOS only)
# ---------------------------------------------------------------------------

ensure_homebrew() {
    if command -v brew >/dev/null 2>&1; then
        step "Homebrew already installed: $(command -v brew)"
        return 0
    fi

    warn "Homebrew is not installed."
    if ! confirm "Install Homebrew now? It will run brew's official installer." "yes"; then
        abort "Homebrew is required on macOS to install Python 3.12."
    fi

    step "running Homebrew's official installer"
    NONINTERACTIVE=1 /bin/bash -c \
        "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    # Homebrew on Apple Silicon installs to /opt/homebrew; on Intel to /usr/local.
    local brew_prefix=""
    if [ -x /opt/homebrew/bin/brew ]; then
        brew_prefix="/opt/homebrew"
    elif [ -x /usr/local/bin/brew ]; then
        brew_prefix="/usr/local"
    fi
    if [ -n "$brew_prefix" ]; then
        eval "$("$brew_prefix/bin/brew" shellenv)"
    fi

    if ! command -v brew >/dev/null 2>&1; then
        abort "Homebrew installation reported success but \`brew\` is not on PATH."
    fi
    success "Homebrew installed."
}

# ---------------------------------------------------------------------------
# Python 3.12
# ---------------------------------------------------------------------------

PYTHON_BIN=""

find_python_3_12() {
    local candidate
    for candidate in python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c \
                'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' \
                >/dev/null 2>&1; then
                PYTHON_BIN="$(command -v "$candidate")"
                return 0
            fi
        fi
    done
    return 1
}

sudo_if_needed() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        abort "this step needs root privileges but neither root nor sudo is available: $*"
    fi
}

ensure_python_3_12() {
    if find_python_3_12; then
        step "Python 3.12 already installed: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
        return 0
    fi

    info "installing Python 3.12 via $PKG_MANAGER"
    case "$PKG_MANAGER" in
        brew)
            brew update
            brew install python@3.12
            ;;
        apt)
            sudo_if_needed env DEBIAN_FRONTEND=noninteractive apt-get update
            # python3.12-venv is a separate package on Debian/Ubuntu.
            sudo_if_needed env DEBIAN_FRONTEND=noninteractive apt-get install -y \
                python3.12 python3.12-venv git ca-certificates
            ;;
        pacman)
            sudo_if_needed pacman -Sy --noconfirm python git
            ;;
    esac

    if ! find_python_3_12; then
        abort "installed python3.12 but cannot find it on PATH afterwards."
    fi
    success "Python 3.12 installed: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
}

# ---------------------------------------------------------------------------
# Clone or update the Cerebro repo
# ---------------------------------------------------------------------------

ensure_clone() {
    if [ -d "$CLONE_DIR/.git" ]; then
        info "Cerebro source already present at $CLONE_DIR"
        if confirm "Update it now (git pull --ff-only)?" "yes"; then
            step "git pull --ff-only"
            git -C "$CLONE_DIR" pull --ff-only
        else
            step "leaving existing clone alone"
        fi
    elif [ -e "$CLONE_DIR" ]; then
        abort "$CLONE_DIR exists but is not a git checkout; refusing to clobber it."
    else
        info "cloning $CEREBRO_REPO_URL into $CLONE_DIR"
        mkdir -p "$(dirname "$CLONE_DIR")"
        git clone "$CEREBRO_REPO_URL" "$CLONE_DIR"
    fi

    if [ -n "$CEREBRO_REPO_REF" ]; then
        step "checking out $CEREBRO_REPO_REF"
        git -C "$CLONE_DIR" fetch --tags origin
        git -C "$CLONE_DIR" checkout "$CEREBRO_REPO_REF"
    fi
}

# ---------------------------------------------------------------------------
# Venv + pip install
# ---------------------------------------------------------------------------

ensure_venv() {
    mkdir -p "$STATE_DIR"

    local need_create=0
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        need_create=1
    elif ! "$VENV_DIR/bin/python" -c \
        'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' \
        >/dev/null 2>&1; then
        warn "existing venv at $VENV_DIR is not Python 3.12; recreating"
        rm -rf "$VENV_DIR"
        need_create=1
    fi

    if [ "$need_create" = "1" ]; then
        info "creating venv at $VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR"
    else
        step "venv already present at $VENV_DIR"
    fi

    info "installing Cerebro into the venv"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV_DIR/bin/python" -m pip install -e "$CLONE_DIR"
}

# ---------------------------------------------------------------------------
# Shim + symlink
# ---------------------------------------------------------------------------

write_shim() {
    mkdir -p "$SHIM_DIR"
    # Heredoc is unquoted so $VENV_DIR is expanded once, baking the venv path
    # into the shim. The runtime expansions stay quoted via single quotes.
    cat >"$SHIM_PATH" <<EOF
#!/usr/bin/env bash
# Cerebro shim. Generated by install.sh; do not edit by hand.
set -euo pipefail
exec "$VENV_DIR/bin/python" -m cerebro "\$@"
EOF
    chmod +x "$SHIM_PATH"
    step "wrote shim at $SHIM_PATH"
}

ensure_symlink() {
    mkdir -p "$BIN_DIR"
    if [ -L "$SYMLINK_PATH" ]; then
        local current_target
        current_target="$(readlink "$SYMLINK_PATH")"
        if [ "$current_target" = "$SHIM_PATH" ]; then
            step "symlink already up to date: $SYMLINK_PATH -> $SHIM_PATH"
            return 0
        fi
        warn "replacing existing symlink $SYMLINK_PATH (was -> $current_target)"
        rm -f "$SYMLINK_PATH"
    elif [ -e "$SYMLINK_PATH" ]; then
        abort "$SYMLINK_PATH exists and is not a symlink; refusing to clobber it."
    fi
    ln -s "$SHIM_PATH" "$SYMLINK_PATH"
    step "symlinked $SYMLINK_PATH -> $SHIM_PATH"
}

# ---------------------------------------------------------------------------
# PATH check
# ---------------------------------------------------------------------------

check_path() {
    case ":$PATH:" in
        *":$BIN_DIR:"*)
            step "$BIN_DIR is already on PATH"
            return 0
            ;;
    esac

    warn "$BIN_DIR is not on your PATH."
    cat <<EOF

Add this line to your shell rc to fix that:

  ${C_BOLD}export PATH="\$HOME/.local/bin:\$PATH"${C_RESET}

For bash, append to ~/.bashrc (Linux) or ~/.bash_profile (macOS):

  echo 'export PATH="\$HOME/.local/bin:\$PATH"' >> ~/.bashrc
  source ~/.bashrc

For zsh (default on macOS), append to ~/.zshrc:

  echo 'export PATH="\$HOME/.local/bin:\$PATH"' >> ~/.zshrc
  source ~/.zshrc

Until you do, invoke Cerebro by its full path: ${C_BOLD}$SYMLINK_PATH${C_RESET}

EOF
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

print_banner() {
    local version
    version="$("$SHIM_PATH" --version 2>/dev/null || echo "unknown")"
    cat <<EOF

${C_GREEN}${C_BOLD}Cerebro is installed.${C_RESET}

  version : $version
  source  : $CLONE_DIR
  venv    : $VENV_DIR
  shim    : $SYMLINK_PATH

Next steps:

  ${C_BOLD}cerebro init${C_RESET}      # interactive vault + plugin setup
  ${C_BOLD}cerebro --help${C_RESET}    # list every command

EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    info "Cerebro bootstrap installer"

    detect_platform

    if [ "$PLATFORM" = "macos" ]; then
        ensure_homebrew
    fi

    ensure_python_3_12

    if ! command -v git >/dev/null 2>&1; then
        abort "git is required but not installed."
    fi

    ensure_clone
    ensure_venv
    write_shim
    ensure_symlink
    check_path
    print_banner
}

main "$@"
