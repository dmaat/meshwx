#!/usr/bin/env bash
# Remove meshwx as installed by deploy/install.sh.
#
#   sudo bash deploy/uninstall.sh           # service, udev rule, code, caches
#   sudo bash deploy/uninstall.sh --purge   # ...plus config, state and the meshwx user
#
# Without --purge, /etc/meshwx (config) and /var/lib/meshwx (state) are kept so a
# later install picks up where this one left off.

set -euo pipefail

SVC_USER=meshwx
UNIT=meshwx.service
PREFIX=/opt/meshwx
CONF_DIR=/etc/meshwx
STATE_DIR=/var/lib/meshwx
CACHE_DIR=/var/cache/meshwx
UNIT_FILE=/etc/systemd/system/$UNIT
UNIT_DROPINS=/etc/systemd/system/$UNIT.d
RULES_FILE=/etc/udev/rules.d/99-meshtastic.rules
WRAPPER=/usr/local/bin/meshwx
WRAPPER_MARK='installed by meshwx deploy/install.sh'

PURGE=0
ASSUME_YES=0

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: sudo bash deploy/uninstall.sh [--purge [--yes]]

Stops and removes the meshwx service, the udev rule, the meshwx command,
$PREFIX and $CACHE_DIR.

  --purge     also delete $CONF_DIR (config, may hold API keys),
              $STATE_DIR (runtime state), local unit overrides and the
              $SVC_USER user
  -y, --yes   do not ask for confirmation (needed for --purge without a terminal)
  -h, --help  show this help
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --purge) PURGE=1 ;;
            -y | --yes) ASSUME_YES=1 ;;
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

confirm_purge() {
    local answer=''
    if [ "$PURGE" = 0 ] || [ "$ASSUME_YES" = 1 ]; then
        return
    fi
    [ -t 0 ] || die "--purge permanently deletes the configuration and state; add --yes to confirm"
    printf 'This permanently deletes %s, %s and the user %s. Continue? [y/N] ' \
        "$CONF_DIR" "$STATE_DIR" "$SVC_USER"
    read -r answer || true
    case $answer in
        y | Y | yes | YES) ;;
        *) die "aborted, nothing was removed" ;;
    esac
}

remove_service() {
    local have_systemd=0
    if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
        have_systemd=1
    fi
    if [ "$have_systemd" = 1 ] && [ -e "$UNIT_FILE" ]; then
        say "Stopping and disabling $UNIT"
        systemctl disable --now "$UNIT" || true
    fi
    rm -f "$UNIT_FILE"
    if [ "$PURGE" = 1 ]; then
        rm -rf "$UNIT_DROPINS"
    fi
    if [ "$have_systemd" = 1 ]; then
        systemctl daemon-reload
        systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    fi
}

remove_udev_rule() {
    if [ ! -e "$RULES_FILE" ]; then
        return
    fi
    say "Removing udev rule $RULES_FILE"
    rm -f "$RULES_FILE"
    if command -v udevadm >/dev/null 2>&1 && [ -d /run/udev ]; then
        udevadm control --reload-rules || true
        udevadm trigger --subsystem-match=tty --action=change || true
    fi
}

remove_wrapper() {
    if [ ! -e "$WRAPPER" ]; then
        return
    fi
    if grep -qsF "$WRAPPER_MARK" "$WRAPPER"; then
        say "Removing the meshwx command $WRAPPER"
        rm -f "$WRAPPER"
    else
        warn "$WRAPPER was not installed by meshwx; leaving it alone"
    fi
}

purge_data() {
    say "Deleting $CONF_DIR and $STATE_DIR"
    rm -rf "$CONF_DIR" "$STATE_DIR"
    if id -u "$SVC_USER" >/dev/null 2>&1; then
        say "Removing user $SVC_USER"
        userdel "$SVC_USER" ||
            warn "could not remove user $SVC_USER (still running a process?); retry: sudo userdel $SVC_USER"
    fi
    if getent group "$SVC_USER" >/dev/null; then
        groupdel "$SVC_USER" || warn "could not remove group $SVC_USER"
    fi
}

main() {
    parse_args "$@"
    [ "$(id -u)" -eq 0 ] || die "please run as root: sudo bash deploy/uninstall.sh"
    confirm_purge
    cd /

    remove_service
    remove_udev_rule
    remove_wrapper
    say "Removing $PREFIX and $CACHE_DIR"
    rm -rf "$PREFIX" "$CACHE_DIR"

    if [ "$PURGE" = 1 ]; then
        purge_data
        say "meshwx has been removed completely"
    else
        say "meshwx has been removed"
        printf '    Kept: %s (config), %s (state) and the %s user.\n' "$CONF_DIR" "$STATE_DIR" "$SVC_USER"
        printf '    To delete them as well: sudo bash deploy/uninstall.sh --purge (from your meshwx checkout)\n'
    fi
}

main "$@"
