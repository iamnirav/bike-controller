"""Bridge a NordicTrack Icon-console bike into gamepad input.

Submodules are imported lazily: the bike half needs `bleak` and the gamepad half
needs `evdev` (Linux only), and neither should be a hard requirement of the
other. This lets the mapping logic be tested anywhere, the probe tools run on
macOS, and the gamepad layer run on a Pi without a BLE stack installed.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                  # for type checkers only
    from .bike import BikeState, IconBike
    from .gamepad import ControllerReader, VirtualGamepad
    from .mapping import Mapper, MappingConfig

_LAZY = {
    "BikeState": ".bike",
    "IconBike": ".bike",
    "VirtualGamepad": ".gamepad",
    "ControllerReader": ".gamepad",
    "Mapper": ".mapping",
    "MappingConfig": ".mapping",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(_LAZY[name], __name__)
    value = getattr(module, name)
    globals()[name] = value                        # cache so we only do this once
    return value
