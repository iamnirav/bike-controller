"""A uinput gamepad that can RECEIVE force feedback, built on ctypes.

python-evdev cannot do this: its UInput exposes no `ff_effects_max` (required at
device creation) and no UI_BEGIN_FF_UPLOAD / UI_END_FF_UPLOAD wrappers, both of
which the kernel demands before a uinput device may accept effects. Without
them, our virtual pad advertises no haptics and every rumble the game sends is
dropped on the floor.

The protocol, once the device exists:

  1. The browser uploads an effect. The kernel hands us an EV_UINPUT event with
     code UI_FF_UPLOAD and a request id.
  2. We ioctl UI_BEGIN_FF_UPLOAD with that id; the kernel fills in the effect.
  3. We read the rumble magnitudes, set retval = 0 to accept, and ioctl
     UI_END_FF_UPLOAD. Skipping this leaves the browser's upload blocked.
  4. Later the game plays it: an EV_FF event whose code is the effect id.

Structures are declared faithfully and ctypes computes the sizes -- the ioctl
numbers encode struct size, so a hand-computed layout that is off by a byte
produces an ioctl the kernel does not recognise.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct

# --- ioctl encoding ---------------------------------------------------------
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _ioc(direction, type_, nr, size):
    return ((direction << _IOC_DIRSHIFT) | (type_ << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _io(type_, nr):
    return _ioc(_IOC_NONE, type_, nr, 0)


def _iow(type_, nr, size):
    return _ioc(_IOC_WRITE, type_, nr, size)


def _iowr(type_, nr, size):
    return _ioc(_IOC_READ | _IOC_WRITE, type_, nr, size)


UINPUT_IOCTL_BASE = ord("U")
UINPUT_MAX_NAME_SIZE = 80

EV_SYN, EV_KEY, EV_ABS, EV_FF = 0x00, 0x01, 0x03, 0x15
EV_UINPUT = 0x0101
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
FF_RUMBLE = 0x50


# --- kernel structures ------------------------------------------------------
class _InputId(ctypes.Structure):
    _fields_ = [("bustype", ctypes.c_uint16), ("vendor", ctypes.c_uint16),
                ("product", ctypes.c_uint16), ("version", ctypes.c_uint16)]


class _UinputSetup(ctypes.Structure):
    _fields_ = [("id", _InputId),
                ("name", ctypes.c_char * UINPUT_MAX_NAME_SIZE),
                ("ff_effects_max", ctypes.c_uint32)]


class _InputAbsinfo(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int32), ("minimum", ctypes.c_int32),
                ("maximum", ctypes.c_int32), ("fuzz", ctypes.c_int32),
                ("flat", ctypes.c_int32), ("resolution", ctypes.c_int32)]


class _UinputAbsSetup(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("absinfo", _InputAbsinfo)]


class _FfReplay(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint16), ("delay", ctypes.c_uint16)]


class _FfTrigger(ctypes.Structure):
    _fields_ = [("button", ctypes.c_uint16), ("interval", ctypes.c_uint16)]


class _FfEnvelope(ctypes.Structure):
    _fields_ = [("attack_length", ctypes.c_uint16), ("attack_level", ctypes.c_uint16),
                ("fade_length", ctypes.c_uint16), ("fade_level", ctypes.c_uint16)]


class _FfConstant(ctypes.Structure):
    _fields_ = [("level", ctypes.c_int16), ("envelope", _FfEnvelope)]


class _FfRamp(ctypes.Structure):
    _fields_ = [("start_level", ctypes.c_int16), ("end_level", ctypes.c_int16),
                ("envelope", _FfEnvelope)]


class _FfCondition(ctypes.Structure):
    _fields_ = [("right_saturation", ctypes.c_uint16),
                ("left_saturation", ctypes.c_uint16),
                ("right_coeff", ctypes.c_int16), ("left_coeff", ctypes.c_int16),
                ("deadband", ctypes.c_uint16), ("center", ctypes.c_int16)]


class _FfPeriodic(ctypes.Structure):
    _fields_ = [("waveform", ctypes.c_uint16), ("period", ctypes.c_uint16),
                ("magnitude", ctypes.c_int16), ("offset", ctypes.c_int16),
                ("phase", ctypes.c_uint16), ("envelope", _FfEnvelope),
                ("custom_len", ctypes.c_uint32),
                ("custom_data", ctypes.POINTER(ctypes.c_int16))]


class _FfRumble(ctypes.Structure):
    _fields_ = [("strong_magnitude", ctypes.c_uint16),
                ("weak_magnitude", ctypes.c_uint16)]


class _FfUnion(ctypes.Union):
    _fields_ = [("constant", _FfConstant), ("ramp", _FfRamp),
                ("periodic", _FfPeriodic), ("condition", _FfCondition * 2),
                ("rumble", _FfRumble)]


class _FfEffect(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint16), ("id", ctypes.c_int16),
                ("direction", ctypes.c_uint16), ("trigger", _FfTrigger),
                ("replay", _FfReplay), ("u", _FfUnion)]


class _UinputFfUpload(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32),
                ("effect", _FfEffect), ("old", _FfEffect)]


class _UinputFfErase(ctypes.Structure):
    _fields_ = [("request_id", ctypes.c_uint32), ("retval", ctypes.c_int32),
                ("effect_id", ctypes.c_uint32)]


UI_DEV_CREATE = _io(UINPUT_IOCTL_BASE, 1)
UI_DEV_DESTROY = _io(UINPUT_IOCTL_BASE, 2)
UI_DEV_SETUP = _iow(UINPUT_IOCTL_BASE, 3, ctypes.sizeof(_UinputSetup))
UI_ABS_SETUP = _iow(UINPUT_IOCTL_BASE, 4, ctypes.sizeof(_UinputAbsSetup))
UI_SET_EVBIT = _iow(UINPUT_IOCTL_BASE, 100, ctypes.sizeof(ctypes.c_int))
UI_SET_KEYBIT = _iow(UINPUT_IOCTL_BASE, 101, ctypes.sizeof(ctypes.c_int))
UI_SET_ABSBIT = _iow(UINPUT_IOCTL_BASE, 103, ctypes.sizeof(ctypes.c_int))
UI_SET_FFBIT = _iow(UINPUT_IOCTL_BASE, 107, ctypes.sizeof(ctypes.c_int))
UI_BEGIN_FF_UPLOAD = _iowr(UINPUT_IOCTL_BASE, 200, ctypes.sizeof(_UinputFfUpload))
UI_END_FF_UPLOAD = _iow(UINPUT_IOCTL_BASE, 201, ctypes.sizeof(_UinputFfUpload))
UI_BEGIN_FF_ERASE = _iowr(UINPUT_IOCTL_BASE, 202, ctypes.sizeof(_UinputFfErase))
UI_END_FF_ERASE = _iow(UINPUT_IOCTL_BASE, 203, ctypes.sizeof(_UinputFfErase))

# struct input_event: timeval (two longs) + type, code, value
_EVENT_FORMAT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)


class FFUInput:
    """uinput gamepad that accepts force feedback from whatever reads it."""

    def __init__(self, name: str, vendor: int, product: int, version: int,
                 bustype: int, buttons, axes, ff_effects_max: int = 16) -> None:
        self.name = name
        self.effects: dict[int, tuple[int, int]] = {}     # id -> (strong, weak)
        self._fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_ABS)
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_FF)
            fcntl.ioctl(self._fd, UI_SET_FFBIT, FF_RUMBLE)
            for code in buttons:
                fcntl.ioctl(self._fd, UI_SET_KEYBIT, code)
            for code, info in axes:
                fcntl.ioctl(self._fd, UI_SET_ABSBIT, code)
                setup = _UinputAbsSetup(
                    code=code,
                    absinfo=_InputAbsinfo(0, info.min, info.max,
                                          info.fuzz, info.flat, 0))
                fcntl.ioctl(self._fd, UI_ABS_SETUP, setup)

            setup = _UinputSetup(
                id=_InputId(bustype=bustype, vendor=vendor,
                            product=product, version=version),
                name=name.encode()[:UINPUT_MAX_NAME_SIZE - 1],
                ff_effects_max=ff_effects_max,
            )
            fcntl.ioctl(self._fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self._fd, UI_DEV_CREATE)
        except Exception:
            os.close(self._fd)
            raise

    def write(self, type_: int, code: int, value: int) -> None:
        os.write(self._fd, struct.pack(_EVENT_FORMAT, 0, 0, type_, code, value))

    def syn(self) -> None:
        self.write(EV_SYN, 0, 0)

    def poll(self) -> list[tuple[int, int]]:
        """Service pending FF traffic. Returns (strong, weak) pairs to play now.

        Non-blocking; safe to call every frame. Upload requests MUST be answered
        or the uploading process stays blocked in its ioctl.
        """
        plays: list[tuple[int, int]] = []
        while True:
            try:
                data = os.read(self._fd, _EVENT_SIZE)
            except BlockingIOError:
                return plays
            except OSError:
                return plays
            if len(data) < _EVENT_SIZE:
                return plays
            _, _, type_, code, value = struct.unpack(_EVENT_FORMAT, data)

            if type_ == EV_UINPUT and code == UI_FF_UPLOAD:
                upload = _UinputFfUpload(request_id=value)
                try:
                    fcntl.ioctl(self._fd, UI_BEGIN_FF_UPLOAD, upload)
                    if upload.effect.type == FF_RUMBLE:
                        self.effects[upload.effect.id] = (
                            upload.effect.u.rumble.strong_magnitude,
                            upload.effect.u.rumble.weak_magnitude,
                        )
                    upload.retval = 0            # accept
                    fcntl.ioctl(self._fd, UI_END_FF_UPLOAD, upload)
                except OSError:
                    pass
            elif type_ == EV_UINPUT and code == UI_FF_ERASE:
                erase = _UinputFfErase(request_id=value)
                try:
                    fcntl.ioctl(self._fd, UI_BEGIN_FF_ERASE, erase)
                    self.effects.pop(erase.effect_id, None)
                    erase.retval = 0
                    fcntl.ioctl(self._fd, UI_END_FF_ERASE, erase)
                except OSError:
                    pass
            elif type_ == EV_FF and value >= 1:
                # The game is playing a previously-uploaded effect.
                magnitudes = self.effects.get(code)
                if magnitudes:
                    plays.append(magnitudes)

    def close(self) -> None:
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)
