"""Virtual gamepad output and real-controller input, both via evdev.

Linux-only. The virtual pad deliberately impersonates a wired Xbox 360 pad
(045e:028e): those IDs are in Chromium's GamepadIdList, so the browser applies
its *standard* mapping and buttons land where games expect them. Verified
working in Chromium on a Pi 5 -- see README.md.
"""

from __future__ import annotations

import contextlib
import time

from evdev import AbsInfo, InputDevice, UInput, ecodes as e, ff, list_devices

from .uinput_ff import FFUInput

VENDOR, PRODUCT, VERSION = 0x045E, 0x028E, 0x0110
DEVICE_NAME = "Microsoft X-Box 360 pad"

STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MIN, TRIGGER_MAX = 0, 255

_STICK = AbsInfo(value=0, min=STICK_MIN, max=STICK_MAX, fuzz=16, flat=128, resolution=0)
_TRIGGER = AbsInfo(value=0, min=TRIGGER_MIN, max=TRIGGER_MAX, fuzz=0, flat=0, resolution=0)
_HAT = AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)

BUTTONS = [
    e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y,
    e.BTN_TL, e.BTN_TR,
    e.BTN_SELECT, e.BTN_START, e.BTN_MODE,
    e.BTN_THUMBL, e.BTN_THUMBR,
]

AXES = {
    e.ABS_X: _STICK, e.ABS_Y: _STICK,
    e.ABS_RX: _STICK, e.ABS_RY: _STICK,
    e.ABS_Z: _TRIGGER, e.ABS_RZ: _TRIGGER,
    e.ABS_HAT0X: _HAT, e.ABS_HAT0Y: _HAT,
}

CAPABILITIES = {e.EV_KEY: BUTTONS, e.EV_ABS: list(AXES.items())}

# Axes whose neutral position is the middle of their range, not zero.
CENTERED_AXES = {e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY}


class VirtualGamepad:
    """A uinput gamepad. Set values, then call sync() once per frame."""

    def __init__(self, force_feedback: bool = False) -> None:
        self.ui = None
        self.ff: FFUInput | None = None
        self.path = "?"

        if force_feedback:
            # python-evdev cannot build an FF-capable uinput device, so this
            # path is hand-rolled over ctypes. If anything about it fails we
            # fall back rather than take the bridge down: rumble passthrough is
            # a luxury, a working gamepad is not.
            try:
                self.ff = FFUInput(DEVICE_NAME, VENDOR, PRODUCT, VERSION,
                                   e.BUS_USB, BUTTONS, list(AXES.items()))
                self.path = self._find_path() or "(ff uinput)"
            except Exception as exc:                   # noqa: BLE001
                print(f"  rumble passthrough unavailable ({type(exc).__name__}: "
                      f"{exc}); falling back to a plain virtual pad")
                self.ff = None

        if self.ff is None:
            self.ui = UInput(
                CAPABILITIES,
                name=DEVICE_NAME,
                vendor=VENDOR,
                product=PRODUCT,
                version=VERSION,
                bustype=e.BUS_USB,
            )
            self.path = self.ui.device.path
        self._buttons: dict[int, int] = {code: 0 for code in BUTTONS}
        self._axes: dict[int, int] = {code: 0 for code in AXES}

    @staticmethod
    def _find_path() -> str | None:
        time.sleep(0.3)                       # let udev create the node
        for path in sorted(list_devices()):
            with contextlib.suppress(Exception):
                if InputDevice(path).name == DEVICE_NAME:
                    return path
        return None

    @property
    def has_force_feedback(self) -> bool:
        return self.ff is not None

    def poll_rumble(self) -> list[tuple[int, int]]:
        """Rumble the game wants played, as (strong, weak) magnitudes.

        Must be called regularly: an unanswered upload request leaves the
        *browser* blocked in its ioctl, not just silent.
        """
        return self.ff.poll() if self.ff is not None else []

    def set_button(self, code: int, pressed: bool) -> None:
        if code in self._buttons:
            self._buttons[code] = 1 if pressed else 0

    def set_axis(self, code: int, value: int) -> None:
        if code in self._axes:
            self._axes[code] = int(value)

    def set_axis_normalised(self, code: int, fraction: float) -> None:
        """Set any axis from a 0.0-1.0 value, respecting that axis's range.

        Triggers span 0..255, sticks span -32768..32767. Mapping a fraction onto
        the trigger range and writing it to a stick would give 255/32767 -- 0.4%
        deflection, indistinguishable from centre.

        Sign convention for sticks: more cadence means more FORWARD, and forward
        is negative on evdev's Y axes.
        """
        fraction = min(1.0, max(0.0, fraction))
        if code in CENTERED_AXES:
            self.set_axis(code, int(-fraction * STICK_MAX))
        else:
            self.set_axis(code, int(TRIGGER_MIN + fraction * (TRIGGER_MAX - TRIGGER_MIN)))

    def neutral(self) -> None:
        """Release everything. This is what a closed gate emits."""
        for code in self._buttons:
            self._buttons[code] = 0
        for code in self._axes:
            self._axes[code] = 0

    def sync(self) -> None:
        writer = self.ff if self.ff is not None else self.ui
        for code, value in self._buttons.items():
            writer.write(e.EV_KEY, code, value)
        for code, value in self._axes.items():
            writer.write(e.EV_ABS, code, value)
        writer.syn()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.neutral()
            self.sync()
        with contextlib.suppress(Exception):
            (self.ff or self.ui).close()

    def __enter__(self) -> "VirtualGamepad":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Rumbler:
    """Plays short haptic cues on the physical controller.

    We hold the device with EVIOCGRAB, which blocks other readers but does not
    stop us writing force-feedback to it.

    Effects are uploaded once and replayed by id; re-uploading per pulse would
    exhaust the device's slots (this pad reports 16).
    """

    # name -> (strong magnitude 0..0xffff, weak magnitude, duration ms)
    CUES = {
        # Rising edges only -- there are deliberately no "off" cues. Buzzing on
        # the way down doubles the haptic traffic while riding without telling
        # you anything you cannot already feel in your legs.
        "max_on":     (0xFFFF, 0x0000, 100),
        "sprint_on":  (0xFFFF, 0xFFFF, 200),
        # Launch feedback: a short ack the moment the code is accepted, and a
        # long confirmation once the browser has actually connected.
        "ack":        (0xFFFF, 0x0000, 120),
        "ok":         (0xFFFF, 0xFFFF, 700),
    }

    def __init__(self, device: InputDevice) -> None:
        self.device = device
        self.available = False
        self._ids: dict[str, int] = {}
        self._passthrough_id = -1           # -1 asks the kernel for a new slot
        if e.FF_RUMBLE not in device.capabilities().get(e.EV_FF, []):
            return
        try:
            for name, (strong, weak, ms) in self.CUES.items():
                effect = ff.Effect(
                    e.FF_RUMBLE, -1, 0,
                    ff.Trigger(0, 0),
                    ff.Replay(ms, 0),
                    ff.EffectType(ff_rumble_effect=ff.Rumble(
                        strong_magnitude=strong, weak_magnitude=weak)),
                )
                self._ids[name] = device.upload_effect(effect)
            self.available = True
        except Exception:                       # noqa: BLE001 - haptics are optional
            self.erase()

    def play(self, name: str) -> None:
        """Fire a cue. Never raises -- haptics must not take the bridge down."""
        if not self.available:
            return
        effect_id = self._ids.get(name)
        if effect_id is None:
            return
        try:
            self.device.write(e.EV_FF, effect_id, 1)
        except Exception:                       # noqa: BLE001 - unplugged mid-pulse
            self.available = False

    def passthrough(self, strong: int, weak: int, duration_ms: int = 250) -> None:
        """Play arbitrary magnitudes, for forwarding the game's own rumble.

        Reuses one effect slot, updated in place: uploading a fresh effect per
        rumble would exhaust the pad's 16 slots within seconds of gameplay.
        """
        if not self.available:
            return
        try:
            effect = ff.Effect(
                e.FF_RUMBLE, self._passthrough_id, 0,
                ff.Trigger(0, 0), ff.Replay(duration_ms, 0),
                ff.EffectType(ff_rumble_effect=ff.Rumble(
                    strong_magnitude=max(0, min(0xFFFF, strong)),
                    weak_magnitude=max(0, min(0xFFFF, weak)))),
            )
            self._passthrough_id = self.device.upload_effect(effect)
            self.device.write(e.EV_FF, self._passthrough_id, 1)
        except Exception:                       # noqa: BLE001 - haptics are optional
            self.available = False

    def erase(self) -> None:
        for effect_id in self._ids.values():
            with contextlib.suppress(Exception):
                self.device.erase_effect(effect_id)
        self._ids.clear()
        self.available = False


class ControllerReader:
    """Reads a physical gamepad and mirrors its state.

    Grabs the device exclusively by default. Without the grab the browser would
    enumerate BOTH the real pad and our virtual one, and could bind to the real
    one -- bypassing the gate entirely and defeating the whole point.
    """

    def __init__(self, path: str, grab: bool = True) -> None:
        self.device = InputDevice(path)
        self.grabbed = False
        if grab:
            try:
                self.device.grab()
            except BaseException:
                # __init__ raising means the caller never gets an object to
                # close, so the fd would leak. A grab can fail with EBUSY when
                # another instance holds it, and this path retries every 2s --
                # leaking there exhausts fds and permanently breaks discovery.
                self.device.close()
                raise
            self.grabbed = True

        # Real pads vary wildly in axis range (Xbox One triggers are 0-1023,
        # 360 triggers 0-255), so capture each axis's real range for rescaling.
        self._ranges: dict[int, tuple[int, int]] = {}
        caps = self.device.capabilities().get(e.EV_ABS, [])
        for code, info in caps:
            self._ranges[code] = (info.min, info.max)

        self.buttons: dict[int, int] = {}
        self.axes: dict[int, int] = {}
        self.rumbler = Rumbler(self.device)

    @staticmethod
    def find() -> str | None:
        """First device that advertises gamepad buttons. Skips our own output.

        Every probe is closed again -- this runs every few seconds while no
        controller is attached, so leaked fds would accumulate indefinitely.
        """
        for path in sorted(list_devices()):
            device = None
            try:
                device = InputDevice(path)
                if device.name == DEVICE_NAME:
                    continue                       # that's our virtual pad
                # BTN_A and BTN_GAMEPAD are the same code; BTN_JOYSTICK is not.
                keys = device.capabilities().get(e.EV_KEY, [])
                if e.BTN_A in keys or e.BTN_JOYSTICK in keys:
                    return path
            except Exception:                      # noqa: BLE001 - probe only
                continue
            finally:
                if device is not None:
                    with contextlib.suppress(Exception):
                        device.close()
        return None

    def rescale(self, code: int, value: int) -> int:
        """Map a raw axis value onto the virtual pad's range for that axis."""
        low, high = self._ranges.get(code, (0, 255))
        if high == low:
            return 0
        fraction = (value - low) / (high - low)
        if code in CENTERED_AXES:
            return int(STICK_MIN + fraction * (STICK_MAX - STICK_MIN))
        if code in (e.ABS_HAT0X, e.ABS_HAT0Y):
            return max(-1, min(1, value))
        return int(TRIGGER_MIN + fraction * (TRIGGER_MAX - TRIGGER_MIN))

    def apply(self, event) -> None:
        if event.type == e.EV_KEY:
            self.buttons[event.code] = 1 if event.value else 0
        elif event.type == e.EV_ABS:
            self.axes[event.code] = self.rescale(event.code, event.value)

    def snapshot(self) -> tuple[dict[int, int], dict[int, int]]:
        """Copy of the current button/axis state.

        The reader is written by one task and read by another. Iterating the
        live dicts is only safe while no `await` appears inside the loop -- a
        landmine for anyone adding one later. These dicts hold under 20 entries,
        so copying costs nothing at 60 Hz and removes the invariant entirely.
        """
        return dict(self.buttons), dict(self.axes)

    def close(self) -> None:
        self.rumbler.erase()
        if self.grabbed:
            with contextlib.suppress(Exception):
                self.device.ungrab()
        with contextlib.suppress(Exception):
            self.device.close()
