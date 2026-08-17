#!/bin/bash
# Undo everything install.sh put on this machine.
#
#   ./uninstall.sh           # remove the install; KEEP config.env and ride logs
#   ./uninstall.sh --purge   # also remove config.env and ride logs (confirms)
#
# Idempotent: safe to run twice, or when things are already gone.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=bike-bridge
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
cd "$REPO"

# shellcheck disable=SC1091
[ -f config.env ] && . ./config.env
: "${DESKTOP_USER:=$(stat -c '%U' "$REPO")}"
# The real home, not /home/<user>: correct for root and for any account
# whose home is elsewhere.
: "${RIDE_LOG_DIR:=$(getent passwd "$DESKTOP_USER" | cut -d: -f6)/bike-rides}"

say()  { printf '\n==> %s\n' "$1"; }
gone() { printf '    removed %s\n' "$1"; }
skip() { printf '    (already gone) %s\n' "$1"; }

say "Service"
if systemctl list-unit-files "$UNIT.service" >/dev/null 2>&1 \
   && systemctl cat "$UNIT" >/dev/null 2>&1; then
    sudo systemctl stop "$UNIT" 2>/dev/null || true
    sudo systemctl disable "$UNIT" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$UNIT.service"
    sudo systemctl daemon-reload
    # Clears a lingering `failed` state, so a later install starts clean.
    sudo systemctl reset-failed "$UNIT" 2>/dev/null || true
    gone "/etc/systemd/system/$UNIT.service (stopped and disabled)"
else
    skip "/etc/systemd/system/$UNIT.service"
fi

say "udev rule"
if [ -f /etc/udev/rules.d/99-bike-controller.rules ]; then
    sudo rm -f /etc/udev/rules.d/99-bike-controller.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger -s input
    gone "/etc/udev/rules.d/99-bike-controller.rules (controller visible to browsers again)"
else
    skip "/etc/udev/rules.d/99-bike-controller.rules"
fi

say "Kernel modules"
if [ -f /etc/modules-load.d/bike-controller.conf ]; then
    sudo rm -f /etc/modules-load.d/bike-controller.conf
    gone "/etc/modules-load.d/bike-controller.conf"
    echo "    note: uinput and joydev stay loaded until reboot. Removing this"
    echo "    only stops them loading next boot -- other software may want them."
else
    skip "/etc/modules-load.d/bike-controller.conf"
fi

say "Generated files in the checkout"
for f in .venv systemd/bike-bridge.service udev/99-bike-controller.rules; do
    if [ -e "$f" ]; then rm -rf "$f"; gone "$f"; else skip "$f"; fi
done

say "Your data"
if [ "$PURGE" -eq 1 ]; then
    echo "    --purge will delete:"
    [ -f config.env ]        && echo "      config.env (your Xbox console ID)"
    [ -d "$RIDE_LOG_DIR" ]   && echo "      $RIDE_LOG_DIR ($(ls "$RIDE_LOG_DIR" 2>/dev/null | wc -l | tr -d ' ') files -- ride history cannot be regenerated)"
    read -rp "    Type 'yes' to confirm: " answer
    if [ "$answer" = "yes" ]; then
        rm -f config.env && gone "config.env"
        rm -rf "$RIDE_LOG_DIR" && gone "$RIDE_LOG_DIR"
    else
        echo "    kept (not confirmed)"
    fi
else
    [ -f config.env ]      && echo "    kept config.env"
    [ -d "$RIDE_LOG_DIR" ] && echo "    kept $RIDE_LOG_DIR"
    echo "    (use --purge to remove these too)"
fi

cat <<DONE

==> Done.

Deliberately NOT touched:
  - this repository (delete the directory yourself if you want it gone)
  - apt packages (python3-evdev, python3-venv) -- other software may use them

Reinstall any time with ./install.sh
DONE
