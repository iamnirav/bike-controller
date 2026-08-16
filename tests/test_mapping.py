"""Tests for the cadence -> gamepad mapping.

Runs with pytest, or standalone:  python3 tests/test_mapping.py

mapping.py has no BLE or evdev dependency precisely so this can run anywhere,
including a Mac with no gamepad support installed.

Time is injected everywhere (`now=`), so these are deterministic and instant --
no sleeping, no wall clock.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.mapping import (      # noqa: E402
    AxisConfig,
    ButtonRule,
    CadenceTracker,
    GateConfig,
    Mapper,
    MappingConfig,
    MovementConfig,
)

TELEMETRY_HZ = 0.87          # what the real console actually delivers
FRAME_HZ = 60.0              # what the bridge's output loop runs at


def make_mapper(**gate_kw) -> Mapper:
    gate = GateConfig(open_rpm=40.0, close_rpm=25.0, grace_seconds=1.5, **gate_kw)
    return Mapper(MappingConfig(
        gate=gate,
        axis=AxisConfig(min_rpm=30.0, max_rpm=90.0),
        buttons=[ButtonRule(name="BTN_TR", min_rpm=80.0)],
    ))


def run(mapper: Mapper, cadence, seconds: float, t0: float = 0.0,
        frame_hz: float = FRAME_HZ, feed: bool = True):
    """Drive the mapper for `seconds`, submitting telemetry at the real rate.

    `cadence` may be a constant or a callable of elapsed time. `feed=False`
    simulates a dead link: frames keep being evaluated, no samples arrive.
    """
    step = 1.0 / frame_hz
    sample_every = 1.0 / TELEMETRY_HZ
    next_sample = 0.0
    elapsed = 0.0
    out = None
    while elapsed <= seconds:
        now = t0 + elapsed
        if feed and elapsed >= next_sample:
            value = cadence(elapsed) if callable(cadence) else cadence
            mapper.submit(value, now=now)
            next_sample += sample_every
        out = mapper.evaluate(now=now)
        elapsed += step
    return out, t0 + elapsed


def test_cold_start_gate_is_closed():
    mapper = make_mapper()
    out = mapper.evaluate(now=0.0)
    assert out.gate_open is False
    assert out.cadence == 0.0
    assert out.axis == 0.0


def test_gate_opens_when_pedalling():
    mapper = make_mapper()
    out, _ = run(mapper, 70.0, seconds=6.0)
    assert out.gate_open is True
    assert 0.55 < out.axis < 0.75, out.axis


def test_gate_closes_after_stopping():
    mapper = make_mapper()
    _, t = run(mapper, 70.0, seconds=6.0)
    out, _ = run(mapper, 0.0, seconds=6.0, t0=t)
    assert out.gate_open is False
    assert out.cadence < 1.0


def test_grace_period_survives_one_slow_stroke():
    """A brief dip below the close threshold must not kill input mid-fight."""
    mapper = make_mapper()
    _, t = run(mapper, 70.0, seconds=6.0)
    out, _ = run(mapper, 0.0, seconds=1.0, t0=t)     # shorter than grace
    assert out.gate_open is True


def test_hysteresis_prevents_chatter():
    """Cadence between close and open must not reopen a closed gate."""
    mapper = make_mapper()
    run(mapper, 0.0, seconds=1.0)
    out, _ = run(mapper, 32.0, seconds=8.0, t0=2.0)   # above close, below open
    assert out.gate_open is False, "gate reopened below --gate-open"


def test_dead_link_closes_the_gate():
    """THE safety invariant: a dead feed must never leave the gate open.

    A stuck-open gate hands a game full control from a stationary bike. This is
    the failure mode the whole design is arranged to avoid.
    """
    mapper = make_mapper()
    _, t = run(mapper, 85.0, seconds=8.0)
    assert mapper.evaluate(now=t).gate_open is True

    out, _ = run(mapper, None, seconds=8.0, t0=t, feed=False)
    assert out.gate_open is False, "dead feed left the gate OPEN"
    assert out.cadence == 0.0


def test_dead_link_closes_within_the_stale_window():
    """Closing must not wait out the grace period on top of staleness."""
    mapper = make_mapper()
    _, t = run(mapper, 85.0, seconds=8.0)

    step = 1.0 / FRAME_HZ
    elapsed = 0.0
    while elapsed < 10.0:
        elapsed += step
        if not mapper.evaluate(now=t + elapsed).gate_open:
            break
    else:
        raise AssertionError("gate never closed after the feed died")

    stale_after = mapper.tracker.stale_after
    assert elapsed <= stale_after + 0.5, (
        f"took {elapsed:.2f}s to close; grace should be bypassed when stale"
    )


def test_stale_decay_is_framerate_independent():
    """Fail-safe timing must not change with the output loop's frame rate.

    A per-call multiply would make this depend on FRAME_RATE, so lowering the
    loop rate would silently change how long a dead link keeps control.
    """
    closing_times = []
    for frame_hz in (10.0, 60.0, 240.0):
        mapper = make_mapper()
        _, t = run(mapper, 85.0, seconds=8.0, frame_hz=frame_hz)
        step = 1.0 / frame_hz
        elapsed = 0.0
        while elapsed < 10.0:
            elapsed += step
            if mapper.tracker.value(now=t + elapsed) < 1.0:
                break
        closing_times.append(elapsed)

    spread = max(closing_times) - min(closing_times)
    assert spread < 0.3, f"decay time varies with frame rate: {closing_times}"


def test_axis_is_clamped_and_monotonic():
    for cadence, expected in ((0.0, 0.0), (30.0, 0.0), (60.0, 0.5), (90.0, 1.0),
                              (200.0, 1.0)):
        mapper = make_mapper()
        out, _ = run(mapper, cadence, seconds=8.0)
        assert abs(out.axis - expected) < 0.08, (cadence, out.axis, expected)


def test_threshold_button_fires_only_above_its_rpm():
    mapper = make_mapper()
    out, t = run(mapper, 60.0, seconds=8.0)
    assert "BTN_TR" not in out.buttons
    out, _ = run(mapper, 95.0, seconds=8.0, t0=t)
    assert "BTN_TR" in out.buttons


def test_tracker_ignores_time_going_backwards():
    tracker = CadenceTracker()
    tracker.submit(70.0, now=100.0)
    tracker.value(now=100.5)
    before = tracker.value(now=99.0)      # clock stepped backwards
    assert before >= 0.0


# --- movement scaling -------------------------------------------------------

def make_movement_mapper(**kw) -> Mapper:
    defaults = dict(enabled=True, source="power", min_value=0.0, max_value=130.0)
    return Mapper(MappingConfig(movement=MovementConfig(**{**defaults, **kw})))


def test_movement_scale_is_linear_in_power():
    for watts, expected in ((0, 0.0), (32.5, 0.25), (65, 0.5), (130, 1.0), (300, 1.0)):
        mapper = make_movement_mapper()
        mapper.submit(60.0, watts, now=1.0)
        out = mapper.evaluate(now=1.0)
        assert abs(out.movement_scale - expected) < 0.01, (watts, out.movement_scale)


def test_movement_scale_is_not_smoothed():
    """Raw passthrough: one sample must move the scale the whole way.

    Deliberate -- the real feel is judged before deciding whether any filter is
    wanted. If someone adds smoothing later, this test should be updated, not
    silently broken.
    """
    mapper = make_movement_mapper()
    mapper.submit(60.0, 0.0, now=1.0)
    assert mapper.evaluate(now=1.0).movement_scale == 0.0
    mapper.submit(60.0, 130.0, now=2.0)
    assert mapper.evaluate(now=2.0).movement_scale == 1.0


def test_dead_feed_zeroes_movement_scale():
    """Fail-safe: a stale link must not leave the stick deflected.

    Without this, a dropped BLE connection freezes the stick at its last value
    and walks the character into a wall indefinitely.
    """
    mapper = make_movement_mapper()
    mapper.submit(80.0, 120.0, now=1.0)
    assert mapper.evaluate(now=1.0).movement_scale > 0.9

    stale = 1.0 + mapper.tracker.stale_after + 0.1
    out = mapper.evaluate(now=stale)
    assert out.movement_scale == 0.0, "stale feed left the stick deflected"
    assert out.sprint is False


def test_movement_floor_clears_the_game_deadzone():
    mapper = make_movement_mapper(min_value=20.0, floor=0.3)
    mapper.submit(60.0, 19.0, now=1.0)
    assert mapper.evaluate(now=1.0).movement_scale == 0.0, "below min must be zero"
    mapper.submit(60.0, 21.0, now=2.0)
    assert mapper.evaluate(now=2.0).movement_scale >= 0.3, "floor not applied"


def test_sprint_fires_above_threshold_only():
    mapper = make_movement_mapper(sprint_at=150.0)
    mapper.submit(80.0, 149.0, now=1.0)
    assert mapper.evaluate(now=1.0).sprint is False
    mapper.submit(90.0, 151.0, now=2.0)
    assert mapper.evaluate(now=2.0).sprint is True


def test_sprint_does_not_chatter_at_the_threshold():
    """Effort wobbling around the threshold must not toggle sprint repeatedly.

    Without hysteresis this buzzes the sprint button several times a second,
    which is both wrong input and, with haptics on, a continuous rumble.
    """
    mapper = make_movement_mapper(sprint_at=100.0)
    mapper.submit(80.0, 101.0, now=1.0)
    assert mapper.evaluate(now=1.0).sprint is True
    # Dip just under the threshold but above the release point.
    mapper.submit(80.0, 97.0, now=2.0)
    assert mapper.evaluate(now=2.0).sprint is True, "sprint dropped inside hysteresis"
    # Now fall clearly below the release point.
    mapper.submit(80.0, 85.0, now=3.0)
    assert mapper.evaluate(now=3.0).sprint is False


def test_at_max_is_hysteretic():
    mapper = make_movement_mapper()          # max_value 130
    mapper.submit(80.0, 130.0, now=1.0)
    assert mapper.evaluate(now=1.0).at_max is True
    mapper.submit(80.0, 128.0, now=2.0)      # 98% -- still latched
    assert mapper.evaluate(now=2.0).at_max is True
    mapper.submit(80.0, 100.0, now=3.0)      # 77% -- released
    assert mapper.evaluate(now=3.0).at_max is False


def test_stale_feed_clears_sprint_and_at_max():
    mapper = make_movement_mapper(sprint_at=100.0)
    mapper.submit(90.0, 140.0, now=1.0)
    out = mapper.evaluate(now=1.0)
    assert out.sprint and out.at_max
    out = mapper.evaluate(now=1.0 + mapper.tracker.stale_after + 0.1)
    assert out.sprint is False and out.at_max is False


def test_movement_disabled_leaves_scale_at_one():
    """Callers multiply unconditionally, so disabled must mean 1.0, not 0.0."""
    mapper = make_mapper()
    mapper.submit(70.0, 100.0, now=1.0)
    assert mapper.evaluate(now=1.0).movement_scale == 1.0


def test_movement_can_be_driven_by_cadence():
    movement = MovementConfig(enabled=True, source="cadence",
                              min_value=0.0, max_value=90.0)
    mapper = Mapper(MappingConfig(movement=movement))
    mapper.submit(45.0, 999.0, now=1.0)
    assert abs(mapper.evaluate(now=1.0).movement_scale - 0.5) < 0.01


def test_gate_disabled_always_passes():
    mapper = Mapper(MappingConfig(gate=GateConfig(enabled=False)))
    assert mapper.evaluate(now=0.0).gate_open is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:            # a broken test must report, not abort
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
