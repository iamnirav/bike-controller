#!/bin/bash
# Launch Xbox Remote Play and click through to the console.
#
# Triggered by the bridge when the Konami code is entered and Remote Play is not
# already up. NOT run at boot: the Pi being powered on does not mean anyone is
# riding.
#
# IMPORTANT: the bridge runs as root (it needs /dev/uinput and EVIOCGRAB), so
# this script is invoked as root and MUST drop to the desktop user. Chromium has
# to run as the owner of the Wayland session, and as root XDG_RUNTIME_DIR points
# at /run/user/0, which does not exist -- the script would die before doing
# anything at all.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$REPO/config.env" ] && . "$REPO/config.env"

# Whoever owns the checkout is the desktop user, unless told otherwise.
: "${DESKTOP_USER:=$(stat -c '%U' "$REPO")}"

if [ "$(id -u)" -eq 0 ]; then
    exec runuser -u "$DESKTOP_USER" -- "$0" "$@"
fi

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

LOG="${XDG_RUNTIME_DIR}/bike-remoteplay.log"
exec >>"$LOG" 2>&1
echo "=== $(date -Is) triggered (running as $(whoami)) ==="

# Chromium must be started BY us: --remote-debugging-port only takes effect at
# launch, so we cannot attach to an instance we did not start.
pkill chromium 2>/dev/null && sleep 2

exec "$REPO/.venv/bin/python" -u "$REPO/tools/remoteplay.py" --timeout 240
