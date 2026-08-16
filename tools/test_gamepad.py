#!/usr/bin/env python3
"""Create a virtual Xbox 360 pad via uinput and verify the browser can see it.

RUN THIS ON THE RASPBERRY PI, BEFORE BUILDING ANYTHING ELSE.

    sudo modprobe uinput joydev
    sudo python3 tools/test_gamepad.py

Then open Chromium to https://hardwaretester.com/gamepad and press a button (the
Gamepad API stays silent until it sees real input). You should see a pad named
"Microsoft X-Box 360 pad" with its left stick sweeping in a circle.

Why this test matters: Chromium on Linux is picky about gamepads. Reading
device/gamepad/udev_gamepad_linux.cc and gamepad_device_linux.cc, it requires
ALL of the following, and a virtual device satisfies them only if built
correctly:

  1. A /dev/input/jsN node must exist -- so the `joydev` module must be loaded.
     Chromium still enumerates via joydev, not evdev alone.
  2. The udev device for jsN must carry the ID_INPUT_JOYSTICK property. udev's
     input_id builtin sets this automatically for devices declaring gamepad
     buttons plus absolute axes, which is why the capability list below matters.
  3. The node must have a parent in the "input" subsystem. For uinput that is
     /sys/devices/virtual/input/inputN, which satisfies the check.
  4. Vendor/product are read from the input parent's id/vendor and id/product
     sysattrs. Declaring 045e:028e makes Chromium apply its *standard* mapping,
     so buttons land in the slots a game expects instead of arbitrary indices.

This script checks 1-3 for you and reports what it finds.

It deliberately re-declares the pad's identity and capabilities rather than
importing them from bike_controller.gamepad: this is the first thing you run on
a bare Pi, and its friendly "install evdev" message matters more here than the
duplication. **Keep the capability list in step with gamepad.py** -- if they
drift, this verifies a device the bridge does not build.
"""

import math
import os
import subprocess
import sys
import time

try:
    from evdev import UInput, AbsInfo, ecodes as e
except ImportError:
    sys.exit("Missing dependency. Run:  pip install evdev   (Linux only)")

# Matches the kernel xpad driver's identity for a wired Xbox 360 controller.
VENDOR, PRODUCT, VERSION = 0x045E, 0x028E, 0x0110
DEVICE_NAME = "Microsoft X-Box 360 pad"

STICK = AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)
TRIGGER = AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)
HAT = AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)

CAPABILITIES = {
    e.EV_KEY: [
        e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y,
        e.BTN_TL, e.BTN_TR,
        e.BTN_SELECT, e.BTN_START, e.BTN_MODE,
        e.BTN_THUMBL, e.BTN_THUMBR,
    ],
    e.EV_ABS: [
        (e.ABS_X, STICK), (e.ABS_Y, STICK),
        (e.ABS_RX, STICK), (e.ABS_RY, STICK),
        (e.ABS_Z, TRIGGER), (e.ABS_RZ, TRIGGER),
        (e.ABS_HAT0X, HAT), (e.ABS_HAT0Y, HAT),
    ],
}


def preflight() -> None:
    if not os.path.exists("/dev/uinput"):
        sys.exit("/dev/uinput does not exist. Run:  sudo modprobe uinput")
    if not os.access("/dev/uinput", os.W_OK):
        sys.exit("/dev/uinput is not writable. Run this with sudo, or add a udev "
                 "rule granting your user access to it.")

    with open("/proc/modules") as fh:
        modules = fh.read()
    if "joydev" not in modules:
        print(
            "WARNING: the `joydev` module does not appear to be loaded.\n"
            "         Chromium enumerates gamepads through /dev/input/jsN, so\n"
            "         without it the pad will exist but stay invisible to the\n"
            "         browser. Fix with:  sudo modprobe joydev\n"
            "         Make it permanent:  echo joydev | sudo tee /etc/modules-load.d/joydev.conf\n"
        )


def report_visibility(before: set[str]) -> None:
    after = {n for n in os.listdir("/dev/input") if n.startswith("js")}
    created = sorted(after - before)

    if not created:
        print(
            "\n!! No new /dev/input/jsN node appeared.\n"
            "   Chromium will NOT see this pad. Load joydev and try again:\n"
            "     sudo modprobe joydev\n"
        )
        return

    node = created[0]
    print(f"\n  created /dev/input/{node}")

    # Ask udev directly whether it tagged the node as a joystick.
    try:
        info = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name=/dev/input/{node}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        print("  (udevadm unavailable -- could not verify ID_INPUT_JOYSTICK)")
        return

    if "ID_INPUT_JOYSTICK=1" in info:
        print("  udev set ID_INPUT_JOYSTICK=1 -- Chromium's filter will accept it.")
    else:
        print(
            "  !! udev did NOT set ID_INPUT_JOYSTICK. Chromium will ignore this pad.\n"
            "     Force it with a rule:\n"
            f'       echo \'SUBSYSTEM=="input", ATTRS{{id/vendor}}=="{VENDOR:04x}", '
            f'ATTRS{{id/product}}=="{PRODUCT:04x}", ENV{{ID_INPUT_JOYSTICK}}="1"\' \\\n'
            "         | sudo tee /etc/udev/rules.d/99-virtual-gamepad.rules\n"
            "       sudo udevadm control --reload-rules\n"
        )


def main() -> int:
    preflight()
    before = {n for n in os.listdir("/dev/input") if n.startswith("js")}

    with UInput(
        CAPABILITIES,
        name=DEVICE_NAME,
        vendor=VENDOR,
        product=PRODUCT,
        version=VERSION,
        bustype=e.BUS_USB,
    ) as ui:
        print(f"Created virtual gamepad: {DEVICE_NAME} ({VENDOR:04x}:{PRODUCT:04x})")
        time.sleep(0.5)  # let udev settle before we look for the node
        report_visibility(before)

        print(
            "\nOpen https://hardwaretester.com/gamepad in Chromium on this Pi.\n"
            "The left stick should sweep a circle and A should blink once a second.\n"
            "Ctrl-C to stop.\n"
        )

        t0 = time.monotonic()
        pressed = False
        try:
            while True:
                t = time.monotonic() - t0
                ui.write(e.EV_ABS, e.ABS_X, int(30000 * math.cos(t)))
                ui.write(e.EV_ABS, e.ABS_Y, int(30000 * math.sin(t)))
                # Ramp the right trigger too -- that is the axis cadence will drive.
                ui.write(e.EV_ABS, e.ABS_RZ, int(127 + 127 * math.sin(t / 2)))

                should_press = (int(t) % 2) == 0
                if should_press != pressed:
                    ui.write(e.EV_KEY, e.BTN_A, 1 if should_press else 0)
                    pressed = should_press

                ui.syn()
                time.sleep(1 / 60)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
