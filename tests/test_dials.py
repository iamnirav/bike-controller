"""Tests for the dial registry.

Runs with pytest, or standalone:  python3 tests/test_dials.py

dials.py has no BLE, evdev or HTTP dependency, so this runs anywhere.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.dials import (       # noqa: E402
    BY_KEY,
    DIALS,
    DialError,
    apply,
    check_consistency,
    coerce,
    format_value,
    read,
)
from bike_controller.mapping import MappingConfig       # noqa: E402


def floor_dial():
    return BY_KEY["MOVEMENT_FLOOR"]


# --- the registry itself ---------------------------------------------------

def test_every_live_path_resolves():
    """A typo'd path would silently tune nothing at all.

    This is the test that earns the dotted-path indirection: without it a new
    dial with `path="movement.floorr"` reads as broken only from the saddle,
    where the slider moves and the bike does not.
    """
    config = MappingConfig()
    for dial in DIALS:
        if dial.live:
            read(config, dial)          # raises AttributeError on a bad path


def test_keys_are_unique():
    keys = [d.key for d in DIALS]
    assert len(keys) == len(set(keys)), f"duplicate dial key in {keys}"


def test_defaults_are_within_their_own_bounds():
    """A dial whose live value is already illegal cannot be re-saved.

    The page reads the running value, and posting it back unchanged must not
    be rejected -- which is what happens if a MappingConfig default sits
    outside the range declared for it here.
    """
    config = MappingConfig()
    for dial in DIALS:
        if not dial.live:
            continue
        value = read(config, dial)
        if value is None:
            assert dial.nullable, f"{dial.key} defaults to None but is not nullable"
            continue
        coerce(dial, value)


# --- coercion --------------------------------------------------------------

def test_coerce_accepts_strings():
    """config.env and an HTML form both hand us strings."""
    assert coerce(floor_dial(), "0.45") == 0.45
    assert coerce(BY_KEY["FRAME_RATE"], "60") == 60


def test_coerce_returns_int_for_int_dials():
    value = coerce(BY_KEY["FRAME_RATE"], 60.0)
    assert isinstance(value, int) and value == 60


def test_int_dials_refuse_a_fraction_rather_than_rounding():
    """--frame-rate 62.5 used to run at 62.5 Hz.

    Silently becoming 62 is a value change the caller never sees; refusing it
    is the honest answer.
    """
    try:
        coerce(BY_KEY["FRAME_RATE"], 62.5)
    except DialError as exc:
        assert "whole number" in str(exc)
        return
    raise AssertionError("frame rate accepted 62.5")


def test_range_errors_explain_themselves():
    """The messages this replaced were the documentation for each bound."""
    try:
        coerce(BY_KEY["FROZEN_AFTER"], 1.0)
    except DialError as exc:
        assert "every time you stop" in str(exc), str(exc)
        return
    raise AssertionError("freeze guard accepted 1.0")


# The bounds the dial table enforces, pinned by value. The table replaced
# hand-written argparse checks, and narrowing one silently turns an existing
# config.env into a bridge that refuses to start -- run-bridge.sh exits
# non-zero and systemd's Restart=on-failure loops on it. Carried BY VALUE, not
# read from DIALS, so changing a bound is a visible edit to this test rather
# than something a test happily follows.
EXPECTED_BOUNDS = {
    "MOVEMENT_MAX": (20.0, 500.0),
    "MOVEMENT_MIN": (0.0, 400.0),
    "MOVEMENT_FLOOR": (0.0, 0.99),
    "SPRINT_AT": (20.0, 600.0),
    "FROZEN_AFTER": (2.5, 120.0),
    "POLL_INTERVAL": (0.001, 0.5),
    "FRAME_RATE": (5, 1000),
    "RUMBLE_PASSTHROUGH": (0, 1),
    "RIDE_LOG": (0, 1),
}


def test_bounds_are_what_we_think_they_are():
    for key, (low, high) in EXPECTED_BOUNDS.items():
        dial = BY_KEY[key]
        assert (dial.minimum, dial.maximum) == (low, high), (
            f"{key} bounds changed to ({dial.minimum}, {dial.maximum}); "
            "an existing config.env outside the new range will refuse to boot")
    assert set(EXPECTED_BOUNDS) == set(BY_KEY), "a dial was added or removed"


def test_the_boundaries_themselves_are_accepted_and_just_past_is_not():
    """The previous version of this test only tried values comfortably inside
    the range, so it could not have failed. Test the edges."""
    for key, (low, high) in EXPECTED_BOUNDS.items():
        dial = BY_KEY[key]
        if dial.kind == "bool":
            continue
        coerce(dial, low)
        coerce(dial, high)
        for outside in (low - dial.step, high + dial.step):
            if dial.disabled_value is not None and outside == dial.disabled_value:
                continue
            try:
                coerce(dial, outside)
            except DialError:
                continue
            raise AssertionError(f"{key} accepted {outside}, outside its range")


def test_slider_values_land_on_their_own_step_grid():
    """An <input type=range> snaps to min + step*n.

    A dial whose real value is off that grid shows a thumb in one place and a
    label reading something else -- which is how POLL_INTERVAL came to display
    0.02 while sitting at 0.021.
    """
    interesting = {
        "MOVEMENT_MAX": [75.0, 100.0],          # config.env / argparse defaults
        "MOVEMENT_MIN": [0.0],
        "MOVEMENT_FLOOR": [0.0, 0.25, 0.5],     # every default this has had
        "SPRINT_AT": [100.0],
        "FROZEN_AFTER": [4.0],
        "POLL_INTERVAL": [0.02, 0.05, 0.2],     # deployed + both measured points
        "FRAME_RATE": [60],
    }
    for key, values in interesting.items():
        dial = BY_KEY[key]
        for value in values:
            steps = (value - dial.minimum) / dial.step
            assert abs(steps - round(steps)) < 1e-9, (
                f"{key}={value} is not reachable on a slider of "
                f"min={dial.minimum} step={dial.step}")


def test_out_of_range_is_refused():
    for bad in (-0.1, 1.0, 5.0):
        try:
            coerce(floor_dial(), bad)
        except DialError:
            continue
        raise AssertionError(f"movement floor accepted {bad}")


def test_nan_and_infinity_are_refused():
    """NaN would reach movement.floor and make the stick silently dead.

    Refused by the range check rather than a separate guard: every comparison
    against NaN is false, so the positive `min <= v <= max` form fails it. That
    is why the check must not be rewritten in the negative form.
    """
    for bad in ("nan", "inf", "-inf", float("nan"), float("inf")):
        try:
            coerce(floor_dial(), bad)
        except DialError:
            continue
        raise AssertionError(f"movement floor accepted {bad!r}")


def test_booleans_are_not_numbers():
    """bool subclasses int, so a JSON true/false would arrive as 1.0/0.0.

    Tested on FROZEN_AFTER, not MOVEMENT_FLOOR: True->1.0 is out of the floor's
    range and would be refused anyway, so that version of this test could not
    fail. FROZEN_AFTER is the one that bites -- False coerces to 0.0, which is
    IN range and means "disabled", so `{"FROZEN_AFTER": false}` would silently
    switch off the freeze guard.
    """
    frozen = BY_KEY["FROZEN_AFTER"]
    for value in (True, False):
        try:
            coerce(frozen, value)
        except DialError:
            continue
        raise AssertionError(
            f"freeze guard accepted {value!r} as a number -- "
            "false would silently disable it")


def test_nullable_dial_accepts_off():
    assert coerce(BY_KEY["SPRINT_AT"], None) is None
    assert coerce(BY_KEY["SPRINT_AT"], "") is None


def test_non_nullable_dial_refuses_off():
    try:
        coerce(floor_dial(), None)
    except DialError:
        return
    raise AssertionError("movement floor accepted None")


def test_disabled_value_sits_outside_the_range():
    """FROZEN_AFTER takes 0 or >= 2.5 and nothing in between.

    Between them the guard would fire every time the rider stops, because the
    console legitimately holds its last reading for about 2s.
    """
    frozen = BY_KEY["FROZEN_AFTER"]
    assert coerce(frozen, 0) == 0.0
    assert coerce(frozen, 4) == 4.0
    for bad in (1.0, 2.4, -1.0):
        try:
            coerce(frozen, bad)
        except DialError:
            continue
        raise AssertionError(f"freeze guard accepted {bad}")


def test_bool_dial_accepts_shell_spellings():
    rumble = BY_KEY["RUMBLE_PASSTHROUGH"]
    assert coerce(rumble, "1") is True
    assert coerce(rumble, "0") is False
    assert coerce(rumble, "true") is True
    try:
        coerce(rumble, "maybe")
    except DialError:
        return
    raise AssertionError("bool dial accepted 'maybe'")


# --- read / apply ----------------------------------------------------------

def test_apply_round_trips_through_a_real_config():
    config = MappingConfig()
    apply(config, floor_dial(), 0.42)
    assert config.movement.floor == 0.42
    assert read(config, floor_dial()) == 0.42


def test_apply_reaches_a_top_level_field():
    config = MappingConfig()
    apply(config, BY_KEY["FROZEN_AFTER"], 6.0)
    assert config.frozen_after == 6.0


def test_apply_refuses_a_restart_only_dial():
    try:
        apply(MappingConfig(), BY_KEY["POLL_INTERVAL"], 0.1)
    except DialError:
        return
    raise AssertionError("a restart-only dial was applied live")


# --- cross-field consistency ----------------------------------------------

def test_consistency_accepts_the_defaults():
    assert check_consistency(MappingConfig()) == []


def test_consistency_catches_inverted_movement_range():
    config = MappingConfig()
    config.movement.min_value = 200.0
    config.movement.max_value = 100.0
    problems = check_consistency(config)
    assert len(problems) == 1 and "MOVEMENT_MAX" in problems[0]


def test_consistency_catches_inverted_gate():
    config = MappingConfig()
    config.gate.open_rpm = 10.0
    config.gate.close_rpm = 40.0
    assert any("chatter" in p for p in check_consistency(config))


def test_consistency_catches_inverted_axis():
    config = MappingConfig()
    config.axis.min_rpm = 90.0
    config.axis.max_rpm = 30.0
    assert any("axis" in p for p in check_consistency(config))


# --- formatting ------------------------------------------------------------

def test_format_matches_how_config_env_spells_things():
    """A round trip must not turn 0.5 into 0.5000000000000001."""
    assert format_value(floor_dial(), 0.5) == "0.5"
    assert format_value(BY_KEY["MOVEMENT_MAX"], 75.0) == "75"
    assert format_value(BY_KEY["FRAME_RATE"], 60) == "60"
    assert format_value(BY_KEY["RUMBLE_PASSTHROUGH"], True) == "1"
    assert format_value(BY_KEY["SPRINT_AT"], None) == ""


def test_run_bridge_passes_every_dial_it_can():
    """A dial the launcher never passes is one the page only pretends to save.

    MOVEMENT_MIN shipped exactly like that: the page applied it live and wrote
    it to config.env, and run-bridge.sh never passed --movement-min, so every
    restart silently reverted it to the argparse default. Nothing caught it,
    because nothing connected the dial table to the launcher.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "tools", "run-bridge.sh")) as handle:
        script = handle.read()
    for dial in DIALS:
        if dial.arg is None:
            continue
        flag = "--" + dial.arg.replace("_", "-")
        assert flag in script, (
            f"{dial.key} is a dial with a CLI flag ({flag}) that "
            "run-bridge.sh never passes, so it cannot survive a restart")
        assert f"${{{dial.key}" in script or f"${dial.key}" in script, (
            f"run-bridge.sh passes {flag} but never reads ${dial.key} "
            "from config.env")


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
