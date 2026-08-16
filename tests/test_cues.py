"""Guards the haptic cue names against silent breakage.

`Rumbler.play()` returns silently when handed an unknown cue name -- deliberate,
since haptics must never take the bridge down. The cost is that a renamed or
deleted cue produces no error, no log line, and no buzz: the feature just stops
existing. That already nearly happened when the "off" cues were removed.

Both files import evdev (Linux-only), so this reads them with `ast` instead of
importing them, and therefore runs anywhere.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMEPAD = os.path.join(ROOT, "bike_controller", "gamepad.py")
BRIDGE = os.path.join(ROOT, "tools", "bridge.py")


def parse(path: str) -> ast.Module:
    with open(path) as fh:
        return ast.parse(fh.read(), filename=path)


def defined_cues() -> dict:
    """Extract Rumbler.CUES without importing evdev."""
    for node in ast.walk(parse(GAMEPAD)):
        if isinstance(node, ast.ClassDef) and node.name == "Rumbler":
            for item in node.body:
                if (isinstance(item, ast.Assign)
                        and any(getattr(t, "id", None) == "CUES" for t in item.targets)):
                    return ast.literal_eval(item.value)
    raise AssertionError("Rumbler.CUES not found in gamepad.py")


def used_cues() -> set:
    """Every string literal passed to .play(...) or rumble(...) in bridge.py."""
    names = set()
    for node in ast.walk(parse(BRIDGE)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        target = (getattr(func, "attr", None) if isinstance(func, ast.Attribute)
                  else getattr(func, "id", None))
        if target not in ("play", "rumble"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def test_every_cue_used_by_the_bridge_exists():
    defined = set(defined_cues())
    used = used_cues()
    missing = used - defined
    assert not missing, (
        f"bridge.py plays cue(s) {sorted(missing)} that Rumbler.CUES does not "
        f"define; these fail silently. Defined: {sorted(defined)}"
    )


def test_bridge_actually_uses_some_cues():
    """Catches the reverse: cues defined but wired to nothing."""
    assert used_cues(), "bridge.py plays no cues at all -- haptics are dead"


def test_cue_values_are_in_range():
    for name, value in defined_cues().items():
        assert len(value) == 3, f"{name}: expected (strong, weak, ms), got {value}"
        strong, weak, ms = value
        assert 0 <= strong <= 0xFFFF, f"{name}: strong magnitude {strong} out of range"
        assert 0 <= weak <= 0xFFFF, f"{name}: weak magnitude {weak} out of range"
        assert 0 < ms <= 5000, f"{name}: duration {ms}ms implausible"
        assert strong or weak, f"{name}: both motors zero -- would be silent"


def test_cue_count_fits_the_device():
    """Effects are uploaded once at acquisition; the pad reports 16 slots."""
    assert len(defined_cues()) <= 16, "more cues than the controller has effect slots"


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
