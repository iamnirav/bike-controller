"""Kernel ABI regression guard for bike_controller/uinput_ff.py.

The ioctl numbers encode struct size, so a field whose type silently changes
produces an ioctl the kernel does not recognise -- invisible on any machine
without /dev/uinput, and on the Pi it surfaces only as "rumble stopped working".

These constants were verified against a running arm64 kernel. Pure computation,
so this runs anywhere.
"""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller import uinput_ff as u          # noqa: E402


def test_struct_sizes_match_the_kernel():
    expected = {
        "_UinputSetup": 92, "_UinputAbsSetup": 28, "_FfEffect": 48,
        "_FfUnion": 32, "_UinputFfUpload": 104, "_UinputFfErase": 12,
    }
    for name, size in expected.items():
        actual = ctypes.sizeof(getattr(u, name))
        assert actual == size, f"{name}: {actual} bytes, kernel expects {size}"


def test_ff_effect_field_offsets():
    for field, offset in (("type", 0), ("id", 2), ("direction", 4),
                          ("trigger", 6), ("replay", 10), ("u", 16)):
        actual = getattr(u._FfEffect, field).offset
        assert actual == offset, f"ff_effect.{field} at {actual}, expected {offset}"


def test_ioctl_numbers():
    expected = {
        "UI_DEV_SETUP": 0x405C5503, "UI_SET_EVBIT": 0x40045564,
        "UI_SET_FFBIT": 0x4004556B, "UI_BEGIN_FF_UPLOAD": 0xC06855C8,
        "UI_END_FF_UPLOAD": 0x406855C9, "UI_BEGIN_FF_ERASE": 0xC00C55CA,
        "UI_END_FF_ERASE": 0x400C55CB,
    }
    for name, value in expected.items():
        actual = getattr(u, name)
        assert actual == value, f"{name} = {actual:#x}, expected {value:#x}"


def test_input_event_is_24_bytes_on_lp64():
    assert u._EVENT_SIZE == ctypes.sizeof(ctypes.c_long) * 2 + 8


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
