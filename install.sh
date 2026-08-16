#!/bin/bash
# Set up bike-controller on a Raspberry Pi. Idempotent; safe to re-run.
#
#   git clone <repo> && cd bike-controller && ./install.sh
#
# Everything machine-specific is derived here rather than configured: paths come
# from wherever you cloned this, the desktop user from whoever owns the checkout,
# and the controller's USB IDs from the pad plugged in right now. The only value
# you have to supply is your Xbox console ID.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(stat -c '%U' "$REPO")"
cd "$REPO"

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "This installs onto Linux (a Raspberry Pi). Not $(uname -s)."
[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root. It will sudo when needed."

say "System packages"
sudo apt-get install -y -q python3-evdev python3-venv

say "Kernel modules"
# joydev is the one that is NOT loaded by default on Pi OS, and without it the
# virtual pad exists but stays invisible to the browser.
sudo modprobe uinput || true
sudo modprobe joydev
printf 'uinput\njoydev\n' | sudo tee /etc/modules-load.d/bike-controller.conf >/dev/null
[ -e /dev/uinput ] || die "/dev/uinput missing even after modprobe uinput."

say "Python environment"
# --system-site-packages so the apt-installed python3-evdev is visible; evdev is
# awkward to build from source on a Pi.
[ -d .venv ] || python3 -m venv --system-site-packages .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

say "Configuration"
if [ ! -f config.env ]; then
    cp config.env.example config.env
    echo "  created config.env"
fi
. ./config.env
if [ -z "${XBOX_CONSOLE_ID:-}" ]; then
    echo "  Your Xbox console ID is needed to start Remote Play."
    echo "  Open https://www.xbox.com/play/consoles, pick your console, and copy"
    echo "  the ID from the URL: https://play.xbox.com/remoteplay/<THIS_PART>"
    read -rp "  Console ID (blank to fill in later): " console_id
    if [ -n "$console_id" ]; then
        sed -i "s|^XBOX_CONSOLE_ID=.*|XBOX_CONSOLE_ID=$console_id|" config.env
        echo "  saved"
    else
        echo "  skipped -- set XBOX_CONSOLE_ID in config.env before riding"
    fi
fi

say "Controller"
# Read the IDs off whatever is plugged in, rather than asking for them.
ids="$(./.venv/bin/python - <<'PY'
from evdev import InputDevice, ecodes as e, list_devices
for path in sorted(list_devices()):
    try:
        d = InputDevice(path)
    except Exception:
        continue
    if d.name == "Microsoft X-Box 360 pad":      # our own virtual pad
        continue
    keys = d.capabilities().get(e.EV_KEY, [])
    if e.BTN_A in keys or e.BTN_JOYSTICK in keys:
        print(f"{d.info.vendor:04x} {d.info.product:04x} {d.name}")
        break
PY
)"
if [ -n "$ids" ]; then
    vendor="$(echo "$ids" | cut -d' ' -f1)"
    product="$(echo "$ids" | cut -d' ' -f2)"
    name="$(echo "$ids" | cut -d' ' -f3-)"
    echo "  found: $name ($vendor:$product)"
    sed -e "s|@VENDOR@|$vendor|" -e "s|@PRODUCT@|$product|" \
        udev/99-bike-controller.rules.template > udev/99-bike-controller.rules
    sudo cp udev/99-bike-controller.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger -s input
    echo "  udev rule installed (hides it from browsers so only the virtual pad is seen)"
else
    echo "  no controller found. Plug one in and re-run to generate the udev rule."
    echo "  Without it the browser sees a phantom pad that never reports input."
fi

say "Service"
sed -e "s|@REPO@|$REPO|g" systemd/bike-bridge.service.template > systemd/bike-bridge.service
sudo cp systemd/bike-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
echo "  installed for checkout at $REPO (desktop user: $USER_NAME)"

say "Tests"
for t in tests/test_*.py; do ./.venv/bin/python "$t" | tail -1; done

cat <<DONE

==> Done.

  Start it:      sudo systemctl start bike-bridge
  On boot:       sudo systemctl enable bike-bridge
  Watch it:      sudo journalctl -u bike-bridge -f

Then get on the bike and enter the Konami code on the controller:
  up up down down left right left right B A

Tuning lives in config.env -- edit and restart, no daemon-reload needed.
DONE
