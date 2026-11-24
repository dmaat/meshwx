#!/usr/bin/env bash
# Install or update meshwx as a systemd service on Raspberry Pi OS (Trixie or
# Bookworm, 32- or 64-bit) or another Debian-based distribution with systemd.
#
#   sudo bash deploy/install.sh              # first install; safe to re-run
#   sudo bash deploy/install.sh --upgrade    # after "git pull": new code, same config
#
# Steps: system packages (only when missing) -> system user "meshwx" (group
# dialout) -> code in /opt/meshwx/src -> virtualenv /opt/meshwx/venv ->
# /etc/meshwx/config.yaml (setup wizard; never overwritten) -> udev rule ->
# systemd unit (enabled and started) -> "meshwx" command in /usr/local/bin.

set -Eeuo pipefail
umask 022

SVC_USER=meshwx
UNIT=meshwx.service
PREFIX=/opt/meshwx
SRC_DIR=$PREFIX/src
VENV=$PREFIX/venv
CONF_DIR=/etc/meshwx
CONFIG=$CONF_DIR/config.yaml
STATE_DIR=/var/lib/meshwx
CACHE_DIR=/var/cache/meshwx
UNIT_FILE=/etc/systemd/system/$UNIT
RULES_FILE=/etc/udev/rules.d/99-meshtastic.rules
WRAPPER=/usr/local/bin/meshwx
WRAPPER_MARK='installed by meshwx deploy/install.sh'  # uninstall.sh looks for it
PIWHEELS_URL=https://www.piwheels.org/simple
APT_PACKAGES=(python3 python3-venv python3-pip)
# Copied to $SRC_DIR; OPTIONAL_ITEMS only when present in the checkout.
COPY_ITEMS=(meshwx pyproject.toml README.md requirements.txt config.example.yaml deploy)
OPTIONAL_ITEMS=(docs LICENSE)

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

START=1
SKIP_APT=0
UPGRADE=0
HAVE_SYSTEMD=0
USE_PIWHEELS=0
WAS_ACTIVE=0
START_WAIT=10      # seconds a fresh start must survive; longer on ARMv6
PY_VER=
MACHINE=$(uname -m)
CONFIG_STATE=kept  # kept | wizard | example | invalid
WORK_TMP=

if [ -t 1 ]; then
    BOLD=$'\e[1m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
    BOLD='' YELLOW='' RED='' RESET=''
fi

say() { printf '%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
note() { printf '    %s\n' "$@"; }
warn() { printf '%sWARNING:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() {
    printf '%sERROR:%s %s\n' "$RED" "$RESET" "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: sudo bash deploy/install.sh [--upgrade] [--no-start] [--skip-apt]

Installs meshwx as a systemd service: code in $PREFIX, configuration in
$CONFIG, state in $STATE_DIR. Safe to run again.

  --upgrade    update an existing installation (run after "git pull"): new code
               and dependencies, same configuration, no setup wizard
  --no-start   install and enable the service but do not (re)start it
  --skip-apt   do not install system packages (${APT_PACKAGES[*]})
  -h, --help   show this help
EOF
}

on_error() {
    printf '%sERROR:%s step failed at line %s (exit code %s). Fix the problem shown above and run the script again.\n' \
        "$RED" "$RESET" "$1" "$2" >&2
    if [ "$WAS_ACTIVE" = 1 ]; then
        printf 'meshwx was stopped for the update; to start it again: sudo systemctl start %s\n' "$UNIT" >&2
    fi
}

cleanup() {
    if [ -n "$WORK_TMP" ]; then
        rm -rf "$WORK_TMP"
    fi
}

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --no-start) START=0 ;;
            --skip-apt) SKIP_APT=1 ;;
            --upgrade) UPGRADE=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                die "unknown option: $1"
                ;;
        esac
        shift
    done
}

check_source() {
    local item
    for item in "${COPY_ITEMS[@]}"; do
        if [ ! -e "$REPO_DIR/$item" ]; then
            die "$REPO_DIR/$item is missing - run this script from a complete meshwx checkout"
        fi
    done
}

detect_platform() {
    local os model='' arch
    # shellcheck disable=SC1091
    os=$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-${NAME:-}}") || os=''
    if [ -r /proc/device-tree/model ]; then
        model=$(tr -d '\0' </proc/device-tree/model)
    fi
    arch=$(dpkg --print-architecture 2>/dev/null || true)
    say "Platform: ${os:-unknown OS}; ${arch:-unknown} userland on $MACHINE${model:+ ($model)}"

    # piwheels.org builds ARMv6/ARMv7 wheels (PyYAML, dbus-fast, protobuf ...)
    # that PyPI lacks. 64-bit (aarch64) and x86 get wheels from PyPI.
    case ${arch:-$MACHINE} in
        armhf | armel | armv6l | armv7l) USE_PIWHEELS=1 ;;
    esac
    if [ "$MACHINE" = armv6l ]; then
        note "ARMv6 CPU (Pi Zero / Zero W / Pi 1): installing the Python packages takes 10-20 minutes."
        START_WAIT=25
    fi
    if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
        HAVE_SYSTEMD=1
    else
        warn "systemd is not running here: the service will be installed but not enabled or started"
    fi
}

install_apt_packages() {
    local pkg status missing=()
    for pkg in "${APT_PACKAGES[@]}"; do
        # shellcheck disable=SC2016
        status=$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)
        if [[ $status != *"ok installed"* ]]; then
            missing+=("$pkg")
        fi
    done
    if [ ${#missing[@]} -eq 0 ]; then
        return
    fi
    command -v apt-get >/dev/null 2>&1 ||
        die "apt-get not found: install ${missing[*]} yourself, then re-run with --skip-apt"
    say "Installing system packages: ${missing[*]}"
    # --no-install-recommends: python3-pip recommends a C toolchain, which is not
    # needed - every dependency has a wheel (PyPI/piwheels) or a pure-Python fallback.
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

check_python() {
    command -v python3 >/dev/null 2>&1 || die "python3 not found (run without --skip-apt to install it)"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
        die "meshwx needs Python 3.9 or newer (found $(python3 -V 2>&1))"
    PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
}

ensure_user() {
    if ! getent group "$SVC_USER" >/dev/null; then
        groupadd --system "$SVC_USER"
    fi
    if ! id -u "$SVC_USER" >/dev/null 2>&1; then
        say "Creating system user $SVC_USER"
        useradd --system --gid "$SVC_USER" --home-dir "$STATE_DIR" --no-create-home \
            --shell /usr/sbin/nologin --comment "meshwx service" "$SVC_USER"
    fi
    if getent group dialout >/dev/null; then
        usermod -a -G dialout "$SVC_USER"
    else
        warn "group dialout does not exist; the service may not be allowed to open the serial port"
    fi
}

ensure_dirs() {
    install -d -m 0755 "$PREFIX"
    install -d -m 0750 -o root -g "$SVC_USER" "$CONF_DIR"
    # Created here (not only by systemd) so meshwx commands work before the first start.
    install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE_DIR" "$CACHE_DIR"
    # Files left behind by meshwx commands run as root would lock the service out.
    chown -R "$SVC_USER:$SVC_USER" "$STATE_DIR" "$CACHE_DIR"
}

stop_for_update() {
    if [ "$HAVE_SYSTEMD" = 1 ] && systemctl is-active --quiet "$UNIT"; then
        say "Stopping meshwx while its files are replaced"
        systemctl stop "$UNIT"
        WAS_ACTIVE=1
    fi
}

copy_code() {
    local stage=$PREFIX/src.new item items=()
    say "Copying meshwx to $SRC_DIR"
    for item in "${COPY_ITEMS[@]}" "${OPTIONAL_ITEMS[@]}"; do
        if [ -e "$REPO_DIR/$item" ]; then
            items+=("$REPO_DIR/$item")
        fi
    done
    rm -rf "$stage"
    install -d -m 0755 "$stage"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude='__pycache__/' --exclude='*.py[cod]' --exclude='.venv/' \
            --exclude='.git/' --exclude='tests/' "${items[@]}" "$stage/"
    else
        cp -a "${items[@]}" "$stage/"
        find "$stage" \( -name __pycache__ -o -name .venv -o -name .git -o -name tests \) -prune \
            -exec rm -rf {} +
        find "$stage" -name '*.py[cod]' -delete
    fi
    chown -R root:root "$stage"
    chmod -R u+rwX,go+rX,go-w "$stage"
    # Swap in one step; also correct when this script runs from $SRC_DIR itself.
    rm -rf "$SRC_DIR"
    mv "$stage" "$SRC_DIR"
}

ensure_venv() {
    # A venv is tied to one Python minor version (e.g. after Bookworm -> Trixie).
    if [ -e "$VENV" ] && [ ! -d "$VENV/lib/python$PY_VER/site-packages" ]; then
        say "Python is now $PY_VER: recreating $VENV"
        rm -rf "$VENV"
    fi
    if [ ! -x "$VENV/bin/python" ]; then
        say "Creating virtualenv $VENV (Python $PY_VER)"
        if ! python3 -m venv "$VENV"; then
            rm -rf "$VENV"
            die "could not create the virtualenv - install python3-venv (sudo apt install python3-venv)"
        fi
    fi
}

piwheels_configured() {
    local cfg
    cfg=$("$VENV/bin/python" -m pip config list 2>/dev/null || true)
    grep -qs piwheels /etc/pip.conf || [[ $cfg == *piwheels* ]]
}

pip_install() {
    local pip=("$VENV/bin/python" -m pip)
    local opts=(--disable-pip-version-check --timeout 120 --retries 10)
    if [ "$USE_PIWHEELS" = 1 ]; then
        # Take an older wheel over building a newer sdist when piwheels lags PyPI.
        opts+=(--prefer-binary)
        if ! piwheels_configured; then
            opts+=(--extra-index-url "$PIWHEELS_URL")
        fi
    fi
    say "Updating pip"
    # TMPDIR on disk: /tmp is a small RAM disk on Trixie.
    TMPDIR=$WORK_TMP "${pip[@]}" install "${opts[@]}" --upgrade pip
    say "Installing meshwx and its Python dependencies into $VENV"
    # SKIP_CYTHON: if dbus-fast (pulled in by meshtastic -> bleak; meshwx never uses
    # Bluetooth) must be built from source, build its pure-Python variant instead
    # of a long Cython compile.
    TMPDIR=$WORK_TMP SKIP_CYTHON=1 "${pip[@]}" install "${opts[@]}" "${SRC_DIR}[gtfs]"
}

create_config() {
    if [ "$UPGRADE" = 0 ] && [ -t 0 ] && [ -t 1 ]; then
        say "Creating $CONFIG with the setup wizard"
        # The wizard's lookups (ZIP, ...) cache into a throwaway dir, not root's home.
        if MESHWX_STATE_DIR=$WORK_TMP/wizard "$VENV/bin/meshwx" setup -c "$CONFIG" && [ -s "$CONFIG" ]; then
            CONFIG_STATE=wizard
            return
        fi
        warn "the setup wizard did not finish; writing the example configuration instead"
    fi
    say "Writing the example configuration to $CONFIG"
    local tmp
    tmp=$(mktemp "$CONF_DIR/.config.yaml.XXXXXX")
    if ! "$VENV/bin/meshwx" example-config >"$tmp" 2>/dev/null || [ ! -s "$tmp" ]; then
        cp "$SRC_DIR/config.example.yaml" "$tmp"
    fi
    mv -f "$tmp" "$CONFIG"
    CONFIG_STATE=example
}

validate_config() {
    local out rc=0 why
    # Load it exactly like the service does; warnings (e.g. channel 0) are shown too.
    out=$("$VENV/bin/python" -I -c '
import sys
from meshwx.config import load_config
from meshwx.errors import ConfigError
try:
    load_config(sys.argv[1])
except ConfigError as exc:
    sys.exit(str(exc))
' "$CONFIG" 2>&1) || rc=$?
    if [ "$rc" = 0 ]; then
        if [ -n "$out" ]; then
            printf '%s\n' "$out" | sed 's/^/    /'
        fi
        return 0
    fi
    warn "$CONFIG is not valid:"
    # The whole error - it may span several lines - without the logged
    # "config: ..." warnings around it.
    why=$(printf '%s\n' "$out" | sed '/^config: /d')
    if [ -z "$why" ]; then
        why="(the check failed with exit code $rc and printed no reason)"
    fi
    printf '%s\n' "$why" | sed 's/^/    /' >&2
    return 1
}

ensure_config() {
    if [ ! -s "$CONFIG" ]; then
        create_config
    elif cmp -s "$CONFIG" "$SRC_DIR/config.example.yaml"; then
        CONFIG_STATE=example  # kept, but never edited
    else
        say "Keeping your configuration $CONFIG"
    fi
    # Readable by the service only: it may contain API keys.
    chown root:"$SVC_USER" "$CONFIG"
    chmod 0640 "$CONFIG"
    if ! validate_config; then
        CONFIG_STATE=invalid
    fi
}

# Install a text file with LF line endings: a checkout made on Windows may have
# CRLF, which systemd and udev would misread.
install_text() {
    local tmp
    tmp=$(mktemp "$2.XXXXXX")
    tr -d '\r' <"$1" >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$2"
}

install_udev_rule() {
    say "Installing udev rule $RULES_FILE"
    install_text "$SRC_DIR/deploy/99-meshtastic.rules" "$RULES_FILE"
    if command -v udevadm >/dev/null 2>&1 && [ -d /run/udev ]; then
        udevadm control --reload-rules || true
        udevadm trigger --subsystem-match=tty --action=change || true
        udevadm settle --timeout=10 || true
    fi
}

install_unit() {
    say "Installing systemd unit $UNIT_FILE"
    install_text "$SRC_DIR/deploy/meshwx.service" "$UNIT_FILE"
    if [ "$HAVE_SYSTEMD" = 1 ]; then
        systemctl daemon-reload
        systemctl enable "$UNIT"
    fi
}

# "meshwx ..." for humans: the CLI as the service user with the service's
# config, state and cache. Run as plain root, "meshwx set" would write a state
# file the running service never reads (and root-owned files it cannot use).
install_wrapper() {
    if [ -e "$WRAPPER" ] && ! grep -qsF "$WRAPPER_MARK" "$WRAPPER"; then
        warn "$WRAPPER exists and was not installed by meshwx; leaving it alone"
        return
    fi
    say "Installing the meshwx command $WRAPPER"
    install -d -m 0755 "$(dirname "$WRAPPER")"
    local tmp
    tmp=$(mktemp "$WRAPPER.XXXXXX")
    cat >"$tmp" <<EOF
#!/bin/sh
# meshwx command - $WRAPPER_MARK (re-created on every install).
# Runs the meshwx CLI as the service user "$SVC_USER" with the service's config,
# state and cache, so "meshwx set zip ..." reaches the running service and no
# root-owned files end up in $STATE_DIR.
# To use the CLI as yourself (own config and state): $VENV/bin/meshwx
set -- env -u MESHWX_STATE_DIR MESHWX_CONFIG=$CONFIG STATE_DIRECTORY=$STATE_DIR \\
    CACHE_DIRECTORY=$CACHE_DIR HOME=$STATE_DIR $VENV/bin/meshwx "\$@"
if [ "\$(id -un)" = $SVC_USER ]; then
    exec "\$@"
elif [ "\$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
    exec runuser -u $SVC_USER -- "\$@"
else
    exec sudo -u $SVC_USER -- "\$@"
fi
EOF
    chmod 0755 "$tmp"
    mv -f "$tmp" "$WRAPPER"
}

start_service() {
    local restarts
    say "Starting meshwx"
    systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    systemctl restart "$UNIT"
    note "Checking that it stays up (${START_WAIT}s) ..."
    sleep "$START_WAIT"
    restarts=$(systemctl show -p NRestarts --value "$UNIT" 2>/dev/null || true)
    if systemctl is-active --quiet "$UNIT" && [ "${restarts:-0}" = 0 ]; then
        note "meshwx is running."
    else
        warn "meshwx did not stay up. Recent log:"
        journalctl -u "$UNIT" -n 25 --no-pager >&2 || true
    fi
}

maybe_start() {
    if [ "$HAVE_SYSTEMD" = 0 ]; then
        return
    fi
    if [ "$START" = 0 ]; then
        note "Not started (--no-start). Start it with: sudo systemctl start meshwx"
        if [ "$WAS_ACTIVE" = 1 ]; then
            note "(meshwx was running before this update and is now stopped.)"
        fi
    elif [ "$CONFIG_STATE" = invalid ]; then
        warn "meshwx was not started because $CONFIG has errors (see above)."
        note "Fix it (sudo nano $CONFIG), then: sudo systemctl start meshwx"
        if [ "$WAS_ACTIVE" = 1 ]; then
            note "(meshwx was running before this update and is now stopped.)"
        fi
    elif [ "$CONFIG_STATE" = example ] && [ "$WAS_ACTIVE" = 0 ]; then
        # The example config would broadcast weather for Boston (02108) on channel 0.
        note "Not started yet: $CONFIG is the example configuration."
        note "Edit it first - at least location.zip_code and broadcast.channel_index:"
        note "    sudo nano $CONFIG"
        note "    sudo systemctl start meshwx"
    else
        if [ "$CONFIG_STATE" = example ]; then
            # Someone started it with these settings on purpose: an update keeps it running.
            note "$CONFIG is the unchanged example configuration, but meshwx was running"
            note "before this update, so it is started again."
        fi
        start_service
    fi
}

summary() {
    printf '\n'
    say "meshwx is installed"
    if [ "$HAVE_SYSTEMD" = 1 ]; then
        systemctl --no-pager --lines=0 status "$UNIT" 2>/dev/null | head -n 3 || true
    fi
    cat <<EOF

  Configuration:     $CONFIG
                     (sudo nano $CONFIG, then: sudo systemctl restart meshwx)
  Follow the log:    journalctl -u meshwx -f
  Preview messages:  meshwx preview
  Change ZIP code:   meshwx set zip 90210
  Change interval:   meshwx set interval 60
                     (a running service picks up "set" changes within ~30 s)
  Show settings:     meshwx status

  The meshwx command ($WRAPPER) runs $VENV/bin/meshwx as the
  $SVC_USER user with the service's config and state, i.e. the same as
    sudo -u $SVC_USER env MESHWX_CONFIG=$CONFIG STATE_DIRECTORY=$STATE_DIR \\
        CACHE_DIRECTORY=$CACHE_DIR $VENV/bin/meshwx ...
  Do not run $VENV/bin/meshwx "set" as root: the service would not see it.

  Update later:      git pull && sudo bash deploy/install.sh --upgrade
  Uninstall:         sudo bash $SRC_DIR/deploy/uninstall.sh
EOF
}

main() {
    parse_args "$@"
    [ "$(id -u)" -eq 0 ] || die "please run as root: sudo bash deploy/install.sh"
    [ "$(uname -s)" = Linux ] || die "this installer is for Linux; elsewhere see README.md (pip install -e .[gtfs])"
    trap 'on_error $LINENO $?' ERR
    trap cleanup EXIT
    check_source
    cd /  # keep the checkout off Python's import path
    if [ "$UPGRADE" = 1 ] && [ ! -x "$VENV/bin/python" ]; then
        die "meshwx is not installed yet - run without --upgrade"
    fi
    # Disk-backed scratch space (pip builds, wizard caches); removed on exit.
    WORK_TMP=$(mktemp -d /var/tmp/meshwx-install.XXXXXX)

    detect_platform
    if [ "$SKIP_APT" = 0 ]; then
        install_apt_packages
    fi
    check_python
    ensure_user
    ensure_dirs
    stop_for_update
    copy_code
    ensure_venv
    pip_install
    ensure_config
    install_udev_rule
    install_unit
    install_wrapper
    maybe_start
    summary
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
