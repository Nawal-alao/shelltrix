#!/bin/sh
#
# shelltrix — cross-platform installer (Linux + macOS; Windows via WSL2).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Nawal-alao/shelltrix/main/install.sh | sh
#
# The script is idempotent: re-running it on an already configured machine
# breaks nothing (it only reinstalls what is missing). It never pipes a sudo
# command without announcing it on screen beforehand.
#
# NOTE PyPI publishing: once shelltrix is published on PyPI, replace the
# install below with `pipx install shelltrix` (and the README curl with the
# PyPI path). The `git+https://...` stays valid until then.

set -e

# ---------------------------------------------------------------------------
# Ref validation
# ---------------------------------------------------------------------------
# A pinned ref is a release tag (v1.0.1, v1.2.0-rc1) or a commit SHA. It is
# NOT restricted to hexadecimal: the pin below is a tag by necessity, since a
# file cannot contain the SHA of the commit that contains it.
# `valid_ref` therefore accepts the tag charset and rejects what could be read
# as an option (`-x`, to dodge a guard into the installer's own arguments) or
# smuggle a second command through the unquoted git URL.
# NOTE: `sh install.sh --check-ref <ref>` runs this check alone, so the test
# suite exercises the real validation instead of a copy of it.
valid_ref() {
    case "$1" in
        ''|-*)                return 1 ;;
        *[!A-Za-z0-9._+-]*)   return 1 ;;
    esac
    return 0
}

if [ "${1:-}" = "--check-ref" ]; then
    if [ -z "${2:-}" ] || ! valid_ref "$2"; then
        exit 1
    fi
    printf 'ref ok: %s\n' "$2"
    exit 0
fi

# ---------------------------------------------------------------------------
# Pinning the source (supply-chain security, H1)
# ---------------------------------------------------------------------------
# The installed version is **pinned** to an immutable ref: the release tag
# `v1.0.3`, set on the last commit of the series. A force-push or a malicious
# commit on `main` can therefore not be deployed by this script: the source
# is verifiable and replayable, and it is *the last published commit* — not a
# commit one version behind.
# Why a tag and not a SHA: a file cannot contain the SHA of the commit that
# contains it (the SHA would change as soon as we write it). The tag removes
# that self-reference: it designates the last commit and never moves again.
# INVARIANT: this tag must NEVER be moved nor deleted.
# Override if needed:            SHELLTRIX_REF=<tag|commit> ./install.sh
# Once the package is published on PyPI, this block is replaced by a
# version-pinned PyPI install (`pipx install shelltrix==x.y.z`).
# Locked down by `test_pinned_ref_installs_hardened_code`.
SHELLTRIX_REF="${SHELLTRIX_REF:-v1.0.3}"

# ---------------------------------------------------------------------------
# Display colors/utilities (POSIX — no bashisms)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    _BOLD='\033[1m'
    _DIM='\033[2m'
    _RED='\033[31m'
    _GREEN='\033[32m'
    _YELLOW='\033[33m'
    _RESET='\033[0m'
else
    _BOLD=''
    _DIM=''
    _RED=''
    _GREEN=''
    _YELLOW=''
    _RESET=''
fi

# ---------------------------------------------------------------------------
# "SHELLTRIX" ASCII banner (fixed block, no generated font). The color is
# handled by the variables above (already "off" outside a tty); only the
# locale tells whether box-drawing characters are safe to print.
# ---------------------------------------------------------------------------
banner() {
    case "${LANG:-}${LC_ALL:-}" in
        *UTF-8*|*utf8*) banner_ok=1 ;;
        *) banner_ok=0 ;;
    esac
    if [ "$banner_ok" = 1 ]; then
        printf '%b' "$_BOLD"
        cat <<'LOGO'

███████╗██╗  ██╗███████╗██╗     ██╗  ████████╗██████╗ ██╗██╗  ██╗
██╔════╝██║  ██║██╔════╝██║     ██║  ╚══██╔══╝██╔══██╗██║╚██╗██╔╝
███████╗███████║█████╗  ██║     ██║     ██║   ██████╔╝██║ ╚███╔╝ 
╚════██║██╔══██║██╔══╝  ██║     ██║     ██║   ██╔══██╗██║ ██╔██╗ 
███████║██║  ██║███████╗███████╗███████╗██║   ██║  ██║██║██╔╝ ██╗
╚══════╝╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝ 

LOGO
        printf '%b\n' "$_RESET"
    else
        printf '%b\n' "${_BOLD}SHELLTRIX${_RESET}"
    fi
}
banner

info()  { printf '%b%b%s%b\n' "$_GREEN" "  • " "$1" "$_RESET"; }
step()  { printf '%b%b%s%b\n' "$_BOLD" "==> " "$1" "$_RESET"; }
warn()  { printf '%b%b%s%b\n' "$_YELLOW" "WARN " "$1" "$_RESET"; }
die()   { printf '%b%b%s%b\n' "$_RED" "ERROR " "$1" "$_RESET" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# The ref is validated HERE, before anything is installed on the system.
# Previously this check sat at step 5, after libolm and pipx had already been
# installed: an invalid ref therefore modified the machine and *then* aborted.
# A typo in SHELLTRIX_REF must cost the user nothing.
if ! valid_ref "$SHELLTRIX_REF"; then
    die "Invalid SHELLTRIX_REF: '$SHELLTRIX_REF' (a release tag like v1.0.1 or a commit SHA is expected)."
fi

# ---------------------------------------------------------------------------
# 1. OS detection
# ---------------------------------------------------------------------------
OS="$(uname -s 2>/dev/null || echo Unknown)"
case "$OS" in
    Darwin) OS_FAMILY="macos" ;;
    Linux)  OS_FAMILY="linux" ;;
    *)
        cat <<EOF

${_RED}Shelltrix requires Linux or macOS.${_RESET}

On Windows, install via WSL2 then re-run this script from your WSL
terminal (Ubuntu preferred):
  https://learn.microsoft.com/windows/wsl/install

EOF
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# 2. Python >= 3.10 check
# ---------------------------------------------------------------------------
step "Checking Python (>= 3.10)"

if ! command_exists python3; then
    if [ "$OS_FAMILY" = "macos" ]; then
        cat <<EOF
${_RED}python3 not found.${_RESET}
Install Python 3.10+:
  https://www.python.org/downloads/macos/
  (or via Homebrew: brew install python)
Then re-run this script.
EOF
    else
        cat <<EOF
${_RED}python3 not found.${_RESET}
Install Python 3.10+ via your distro's package manager
(e.g. 'sudo apt install python3' on Debian/Ubuntu), then re-run this
script.
EOF
    fi
    exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
PY_MAJOR="$(printf '%s' "$PY_VERSION" | cut -d. -f1)"
PY_MINOR="$(printf '%s' "$PY_VERSION" | cut -d. -f2)"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    cat <<EOF
${_RED}Python $PY_VERSION is too old: shelltrix needs Python >= 3.10.${_RESET}
Upgrade Python then re-run this script.
EOF
    exit 1
fi
info "Python $PY_VERSION detected (>= 3.10): OK"

# ---------------------------------------------------------------------------
# 3. libolm — the critical dependency (E2EE)
# ---------------------------------------------------------------------------
step "Checking libolm"

if [ "$OS_FAMILY" = "macos" ]; then
    if ! command_exists brew; then
        cat <<EOF
${_RED}Homebrew not found (required to install libolm).${_RESET}
Install Homebrew:   https://brew.sh
Then re-run this script.
EOF
        exit 1
    fi
    if brew list libolm >/dev/null 2>&1; then
        info "libolm already installed: OK"
    else
        echo "  ${_YELLOW}Installing libolm via Homebrew (no sudo required)…${_RESET}"
        brew install libolm
        info "libolm installed"
    fi
else
    # Linux: identify the distro via /etc/os-release
    . /etc/os-release 2>/dev/null || { . /usr/lib/os-release 2>/dev/null || DISTRO_ID="unknown"; }
    DISTRO_ID="${ID:-unknown}"

    case "$DISTRO_ID" in
        debian|ubuntu|pop|linuxmint|elementary)
            if dpkg -s libolm-dev >/dev/null 2>&1; then
                info "libolm-dev already installed: OK"
            else
                echo "  ${_YELLOW}Installing libolm-dev via apt (sudo required)…${_RESET}"
                sudo apt-get update
                sudo apt-get install -y libolm-dev
                info "libolm-dev installed"
            fi
            ;;
        fedora|rhel|centos|rocky|almalinux)
            if rpm -q libolm-devel >/dev/null 2>&1; then
                info "libolm-devel already installed: OK"
            else
                echo "  ${_YELLOW}Installing libolm-devel via dnf (sudo required)…${_RESET}"
                sudo dnf install -y libolm-devel
                info "libolm-devel installed"
            fi
            ;;
        arch|manjaro|endeavouros)
            if pacman -Q libolm >/dev/null 2>&1; then
                info "libolm already installed: OK"
            else
                echo "  ${_YELLOW}Installing libolm via pacman (sudo required)…${_RESET}"
                sudo pacman -S --noconfirm libolm
                info "libolm installed"
            fi
            ;;
        *)
            cat <<EOF
${_RED}Unrecognized Linux distro: install libolm manually.${_RESET}
Shelltrix (E2EE) needs libolm. Refer to the build matrix instructions of
matrix-org/olm:
  https://github.com/matrix-org/olm
Then re-run this script (or install shelltrix by another method).
EOF
            exit 1
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# 4. pipx (or uv if already present)
# ---------------------------------------------------------------------------
step "Checking pipx / uv"

# pipx installs scripts into ~/.local/bin (added to the PATH via ensurepath).
# We prepend it right away so shelltrix is found just after installation.
LOCAL_BIN="$HOME/.local/bin"
PATH="$LOCAL_BIN:$PATH"
export PATH

# On Linux, Debian 11+ (and most distros) blocks global pip installs via
# PEP 668 ("externally-managed-environment"). pipx via the package manager
# sidesteps that block cleanly. macOS uses the official pipx binary
# (no sudo required).
install_pipx() {
    if [ "$OS_FAMILY" = "macos" ]; then
        echo "  ${_YELLOW}Installing pipx via Homebrew (no sudo required)…${_RESET}"
        brew install pipx
    else
        echo "  ${_YELLOW}Installing pipx via the package manager (sudo required)…${_RESET}"
        # pipx is shipped by several distros; we go through the archive
        # utility whichever the manager, trying idempotently.
        if command_exists apt-get; then
            sudo apt-get install -y pipx
        elif command_exists dnf; then
            sudo dnf install -y pipx
        elif command_exists pacman; then
            sudo pacman -S --noconfirm python-pipx
        else
            die "pipx absent and no known package manager: install pipx manually (https://pipx.pypa.io/)."
        fi
    fi
    if command_exists pipx; then
        pipx ensurepath >/dev/null 2>&1 || true
    fi
}

INSTALLER=""
if command_exists uv; then
    INSTALLER="uv"
    info "uv detected: using 'uv tool install' (faster)"
elif command_exists pipx; then
    INSTALLER="pipx"
    info "pipx already present: OK"
else
    INSTALLER="pipx"
    install_pipx
    if command_exists pipx; then
        info "pipx installed"
    else
        die "pipx could not be installed: re-run the script after installing pipx manually."
    fi
fi

# ---------------------------------------------------------------------------
# 5. Installing shelltrix
# ---------------------------------------------------------------------------
step "Installing shelltrix (pinned to ${SHELLTRIX_REF})"

# Re-validated here, at the point of use: `valid_ref` was checked before the
# system was touched (see above), and a ref that passed then still has to be
# rejected rather than silently accepted if this script is edited later.
if ! valid_ref "$SHELLTRIX_REF"; then
    die "Invalid SHELLTRIX_REF: '$SHELLTRIX_REF' (a release tag like v1.0.1 or a commit SHA is expected)."
fi

install_shelltrix() {
    # NOTE PyPI publishing: replace the pinned git+https with
    # `pipx install shelltrix==x.y.z` (or `uv tool install shelltrix==x.y.z`)
    # once the package is published.
    SHELLTRIX_SOURCE="git+https://github.com/Nawal-alao/shelltrix.git@${SHELLTRIX_REF}"
    if [ "$INSTALLER" = "uv" ]; then
        uv tool install "$SHELLTRIX_SOURCE"
    else
        pipx install "$SHELLTRIX_SOURCE"
    fi
}

if command_exists shelltrix; then
    info "shelltrix already installed: OK"
    SHELLTRIX_VERSION="$(shelltrix --version 2>/dev/null || echo unknown)"
    warn "shelltrix present (current version: $SHELLTRIX_VERSION) — to upgrade it: pipx upgrade shelltrix (or uv tool upgrade shelltrix)."
else
    install_shelltrix
    info "shelltrix installed"
fi

# ---------------------------------------------------------------------------
# 6. Message final
# ---------------------------------------------------------------------------
cat <<EOF

${_BOLD}Installation complete.${_RESET}
  • Launch shelltrix with:       ${_BOLD}shelltrix${_RESET}
  • Check the version:           ${_BOLD}shelltrix --version${_RESET}
  • Config:                      ~/.config/shelltrix/
  • If 'shelltrix' is not found, reopen your terminal (pipx has added
    ~/.local/bin to your PATH).

EOF
