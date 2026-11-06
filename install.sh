#!/usr/bin/env bash
# usbkill-ng installer.      Usage: sudo ./install.sh [--dry-run]
# Undo it with:              sudo ./uninstall.sh
# It installs the files and the systemd unit but does NOT enable or start the service.
# DESTDIR=/some/folder stages the install into a folder instead of the real system.
set -euo pipefail

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTDIR="${DESTDIR:-}"
APP="$DESTDIR/opt/usbkill-ng"
BIN="$DESTDIR/usr/local/bin/usbkill-ng"
UNIT="$DESTDIR/etc/systemd/system/usbkill-ng.service"
LOGROTATE="$DESTDIR/etc/logrotate.d/usbkill-ng"
CONF_DIR="$DESTDIR/etc/usbkill"
CONF="$CONF_DIR/usbkill.ini"
PY=/usr/bin/python3

say() { echo "[install] $*"; }
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

if [ -z "$DESTDIR" ] && [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "[install] must run as root: sudo ./install.sh" >&2; exit 1
fi
[ -x "$PY" ] || { echo "[install] $PY not found" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || { echo "[install] Python 3.8 or newer is required" >&2; exit 1; }
[ -d "$SRC/usbkill_ng" ] && [ -f "$SRC/packaging/usbkill-ng.service" ] \
  || { echo "[install] run this from the unpacked usbkill-ng folder" >&2; exit 1; }
command -v loginctl >/dev/null || say "warning: loginctl not found (the lock action needs systemd-logind)"
"$PY" -c 'import pyudev' 2>/dev/null || say "note: python3-pyudev not installed; polling is used (optional: apt install python3-pyudev)"

say "program   -> $APP"
run rm -rf "$APP/usbkill_ng"
run mkdir -p "$APP"
run cp -r "$SRC/usbkill_ng" "$APP/usbkill_ng"
run find "$APP" -name __pycache__ -prune -exec rm -rf {} +
for doc in README.md LICENSE NOTICE; do [ -f "$SRC/$doc" ] && run cp "$SRC/$doc" "$APP/$doc"; done
# root runs this code, so it must be root-owned and not writable by anyone else
[ -z "$DESTDIR" ] && run chown -R root:root "$APP"
run chmod -R go-w "$APP"

say "command   -> $BIN"
if [ "$DRY" = 1 ]; then
  echo "[dry-run] write $BIN"
else
  mkdir -p "$(dirname "$BIN")"
  printf '%s\n' '#!/bin/sh' 'exec env PYTHONPATH=/opt/usbkill-ng /usr/bin/python3 -m usbkill_ng "$@"' > "$BIN"
  chmod 755 "$BIN"
fi

say "service   -> $UNIT (not enabled, not started)"
run install -D -m 644 "$SRC/packaging/usbkill-ng.service" "$UNIT"

say "logrotate-> $LOGROTATE"
run install -D -m 644 "$SRC/packaging/usbkill-ng.logrotate" "$LOGROTATE"

if [ -e "$CONF" ]; then
  say "config    -> keeping your existing $CONF"
  say "            new template written to $CONF.dist (compare with: diff $CONF $CONF.dist)"
  run rm -f "$CONF.dist"
  if [ "$DRY" = 1 ]; then echo "[dry-run] write $CONF.dist"; else
    PYTHONPATH="$SRC" "$PY" -m usbkill_ng --init-config --config "$CONF.dist" >/dev/null; fi
else
  say "config    -> $CONF (new)"
  if [ "$DRY" = 1 ]; then echo "[dry-run] write $CONF"; else
    PYTHONPATH="$SRC" "$PY" -m usbkill_ng --init-config --config "$CONF" >/dev/null; fi
fi

[ -z "$DESTDIR" ] && command -v systemctl >/dev/null && run systemctl daemon-reload

say "done. Next steps:"
say "  usbkill-ng --status                      # service, pause state, config check"
say "  usbkill-ng --show-config                 # check the settings"
say "  sudo systemctl start usbkill-ng          # run it now (not at boot)"
say "  sudo systemctl status usbkill-ng         # see that it is running"
say "  sudo systemctl enable usbkill-ng         # ONLY after testing: start at every boot"
