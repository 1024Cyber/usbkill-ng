#!/usr/bin/env bash
# usbkill-ng uninstaller.    Usage: sudo ./uninstall.sh [--dry-run] [--purge]
# Removes everything install.sh added. Your config and logs are kept unless you add --purge.
# DESTDIR=/some/folder works on a staged install (used by the tests).
set -euo pipefail

DRY=0; PURGE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

DESTDIR="${DESTDIR:-}"
APP="$DESTDIR/opt/usbkill-ng"
BIN="$DESTDIR/usr/local/bin/usbkill-ng"
UNIT="$DESTDIR/etc/systemd/system/usbkill-ng.service"
LOGROTATE="$DESTDIR/etc/logrotate.d/usbkill-ng"
CONF_DIR="$DESTDIR/etc/usbkill"
LOG_DIR="$DESTDIR/var/log/usbkill"

say() { echo "[uninstall] $*"; }
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

if [ -z "$DESTDIR" ] && [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "[uninstall] must run as root: sudo ./uninstall.sh" >&2; exit 1
fi

if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null; then
  say "stopping and disabling the service (if present)"
  run systemctl stop usbkill-ng.service 2>/dev/null || true
  run systemctl disable usbkill-ng.service 2>/dev/null || true
fi

say "removing program, command, service file and logrotate rule"
run rm -f "$UNIT" "$BIN" "$LOGROTATE"
run rm -rf "$APP"
[ -z "$DESTDIR" ] && command -v systemctl >/dev/null && run systemctl daemon-reload

if [ "$PURGE" = 1 ]; then
  say "purging config and logs"
  run rm -rf "$CONF_DIR" "$LOG_DIR"
else
  say "kept $CONF_DIR and $LOG_DIR (add --purge to delete them)"
fi
say "done"
