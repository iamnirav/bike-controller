#!/bin/bash
# Set up bike-controller on a Raspberry Pi. Idempotent; re-run after every pull.
#
#   git clone <repo> && cd bike-controller && ./install.sh    # first time
#   git pull && ./install.sh                                  # every update
#
# Most steps are needed only once, and this skips them when they are already
# done -- so re-running is cheap and you never have to remember which parts of
# an update require which step. It reports what it did and what it skipped.
#
# Everything machine-specific is derived rather than configured: paths from
# wherever you cloned this, the desktop user from whoever owns the checkout, the
# controller's USB IDs from the pad plugged in right now. The only value you
# must supply is your Xbox console ID.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(stat -c '%U' "$REPO")"
UNIT=bike-bridge
cd "$REPO"

say()  { printf '\n==> %s\n' "$1"; }
did()  { printf '    %s\n' "$1"; DID+=("$1"); }
skip() { printf '    (unchanged) %s\n' "$1"; }
die()  { printf '\nERROR: %s\n' "$1" >&2; exit 1; }
DID=()

[ "$(uname -s)" = "Linux" ] || die "This installs onto Linux (a Raspberry Pi). Not $(uname -s)."
[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root. It will sudo when needed."

was_active=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && was_active=1

say "System packages"
missing=()
for pkg in python3-evdev python3-venv; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ ${#missing[@]} -gt 0 ]; then
    sudo apt-get install -y -q "${missing[@]}"
    did "installed ${missing[*]}"
else
    skip "python3-evdev, python3-venv"
fi

say "Kernel modules"
# joydev is the one NOT loaded by default on Pi OS. Without it the virtual pad
# exists but stays invisible to the browser.
for mod in uinput joydev; do
    lsmod | grep -q "^$mod " || sudo modprobe "$mod"
done
[ -e /dev/uinput ] || die "/dev/uinput missing even after modprobe uinput."
want="uinput
joydev"
if [ "$(cat /etc/modules-load.d/bike-controller.conf 2>/dev/null)" != "$want" ]; then
    printf 'uinput\njoydev\n' | sudo tee /etc/modules-load.d/bike-controller.conf >/dev/null
    did "persisted uinput, joydev for next boot"
else
    skip "/etc/modules-load.d/bike-controller.conf"
fi

say "Python environment"
if [ ! -d .venv ]; then
    # --system-site-packages so the apt-installed python3-evdev is visible;
    # evdev is awkward to build from source on a Pi.
    python3 -m venv --system-site-packages .venv
    did "created .venv"
fi
# Reinstall only when requirements have actually moved.
if [ ! -f .venv/.requirements-stamp ] || [ requirements.txt -nt .venv/.requirements-stamp ]; then
    ./.venv/bin/pip install -q --upgrade pip
    ./.venv/bin/pip install -q -r requirements.txt
    touch .venv/.requirements-stamp
    did "installed Python dependencies"
else
    skip "Python dependencies"
fi

say "Configuration"
if [ ! -f config.env ]; then
    cp config.env.example config.env
    did "created config.env"
fi
# shellcheck disable=SC1091
. ./config.env
if [ -z "${XBOX_CONSOLE_ID:-}" ]; then
    echo "    Your Xbox console ID is needed to start Remote Play."
    echo "    Open https://www.xbox.com/play/consoles, pick your console, and copy"
    echo "    the ID from the URL: https://play.xbox.com/remoteplay/<THIS_PART>"
    read -rp "    Console ID (blank to fill in later): " console_id
    if [ -n "$console_id" ]; then
        sed -i "s|^XBOX_CONSOLE_ID=.*|XBOX_CONSOLE_ID=$console_id|" config.env
        did "saved console ID"
    else
        echo "    skipped -- set XBOX_CONSOLE_ID in config.env before riding"
    fi
else
    skip "config.env"
fi

say "Controller"
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
    vendor="${ids%% *}"; rest="${ids#* }"; product="${rest%% *}"; name="${rest#* }"
    sed -e "s|@VENDOR@|$vendor|" -e "s|@PRODUCT@|$product|" \
        udev/99-bike-controller.rules.template > /tmp/bike-udev.$$
    if ! cmp -s /tmp/bike-udev.$$ /etc/udev/rules.d/99-bike-controller.rules 2>/dev/null; then
        mv /tmp/bike-udev.$$ udev/99-bike-controller.rules
        sudo cp udev/99-bike-controller.rules /etc/udev/rules.d/
        sudo udevadm control --reload-rules
        sudo udevadm trigger -s input
        did "udev rule for $name ($vendor:$product)"
    else
        mv /tmp/bike-udev.$$ udev/99-bike-controller.rules
        skip "udev rule ($name)"
    fi
else
    echo "    no controller found. Plug one in and re-run to generate the udev rule."
    echo "    Without it the browser also sees the physical pad, as a phantom that"
    echo "    never reports input."
fi

say "Service"
sed "s|@REPO@|$REPO|g" systemd/bike-bridge.service.template > systemd/bike-bridge.service
if ! cmp -s systemd/bike-bridge.service /etc/systemd/system/$UNIT.service; then
    sudo cp systemd/bike-bridge.service /etc/systemd/system/
    sudo systemctl daemon-reload
    did "installed $UNIT.service for $REPO"
else
    skip "$UNIT.service"
fi

say "Tests"
for t in tests/test_*.py; do ./.venv/bin/python "$t" | tail -1 | sed 's/^/    /'; done

./tools/selftest.sh smoke

if [ "$was_active" -eq 1 ]; then
    say "Restarting (it was running before)"
    sudo systemctl restart "$UNIT"
    sleep 12
    ./tools/selftest.sh health
fi

say "Summary"
if [ ${#DID[@]} -eq 0 ]; then
    echo "    nothing to change -- already up to date"
else
    printf '    %s\n' "${DID[@]}"
fi

if [ "$was_active" -eq 0 ]; then
cat <<DONE

  Start it:   sudo systemctl start $UNIT
  On boot:    sudo systemctl enable $UNIT
  Watch it:   sudo journalctl -u $UNIT -f

Then get on the bike and enter the Konami code on the controller:
  up up down down left right left right B A
DONE
fi

echo
echo "Tuning lives in config.env -- edit, then: sudo systemctl restart $UNIT"
echo "Remove everything this installed with: ./uninstall.sh"
