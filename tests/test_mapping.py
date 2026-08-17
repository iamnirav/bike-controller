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
    stale_after_for,
    ButtonRule,
    CadenceTracker,
    GateConfig,
    Mapper,
    MappingConfig,
    MovementConfig,
)

# 0.87 Hz was the original poll rate; the deployed rate is now 2.56 Hz. Tests
# default to the SLOWER one because it is the harder case for the fail-safe, but
# the deployed rate is exercised explicitly below -- a fail-safe sized in missed
# frames behaves differently when the frame rate triples.
TELEMETRY_HZ = 0.87
DEPLOYED_HZ = 2.56
FRAME_HZ = 60.0              # what the bridge's output loop runs at


def make_mapper(**gate_kw) -> Mapper:
    gate = GateConfig(open_rpm=40.0, close_rpm=25.0, grace_seconds=1.5, **gate_kw)
    return Mapper(MappingConfig(
        gate=gate,
        axis=AxisConfig(min_rpm=30.0, max_rpm=90.0),
        buttons=[ButtonRule(name="BTN_TR", min_rpm=80.0)],
    ))


def run(mapper: Mapper, cadence, seconds: float, t0: float = 0.0,
        frame_hz: float = FRAME_HZ, feed: bool = True,
        telemetry_hz: float = TELEMETRY_HZ, power=None):
    """Drive the mapper for `seconds`, submitting telemetry at the real rate.

    `cadence` and `power` may each be a constant or a callable of elapsed time.
    `power` defaults to the console's own estimate at resistance 0, which we
    measured as watts = 2 * (cadence - 25) -- so movement-scaling tests get a
    plausible power signal instead of a constant zero.

    `feed=False` simulates a dead link: frames keep being evaluated, no samples
    arrive.
    """
    step = 1.0 / frame_hz
    sample_every = 1.0 / telemetry_hz
    next_sample = 0.0
    elapsed = 0.0
    out = None
    while elapsed <= seconds:
        now = t0 + elapsed
        if feed and elapsed >= next_sample:
            value = cadence(elapsed) if callable(cadence) else cadence
            if power is None:
                watts = max(0.0, 2.0 * (value - 25.0))
            else:
                watts = power(elapsed) if callable(power) else power
            mapper.submit(value, watts, now=now)
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


def test_dead_link_closes_the_gate_at_the_deployed_telemetry_rate():
    """The fail-safe is sized in missed frames, so re-check at the real rate."""
    mapper = make_mapper()
    _, t = run(mapper, 85.0, seconds=8.0, telemetry_hz=DEPLOYED_HZ)
    assert mapper.evaluate(now=t).gate_open is True

    out, _ = run(mapper, None, seconds=8.0, t0=t, feed=False,
                 telemetry_hz=DEPLOYED_HZ)
    assert out.gate_open is False, "dead feed left the gate OPEN at 2.56 Hz"
    assert out.cadence == 0.0


def test_movement_scale_zeroes_on_dead_link_at_deployed_rate():
    """Mirrors the gate twin above -- must actually drive the deployed rate."""
    movement = MovementConfig(enabled=True, source="power",
                              min_value=0.0, max_value=75.0)
    mapper = Mapper(MappingConfig(movement=movement))

    _, t = run(mapper, 70.0, seconds=8.0, telemetry_hz=DEPLOYED_HZ)
    assert mapper.evaluate(now=t).movement_scale > 0.9

    out, _ = run(mapper, None, seconds=8.0, t0=t, feed=False,
                 telemetry_hz=DEPLOYED_HZ)
    assert out.movement_scale == 0.0, "dead feed left the stick deflected"


def test_dropped_frames_do_not_kill_movement_at_any_poll_rate():
    """A single dropped BLE frame must not zero movement mid-ride.

    This is the regression that tightening stale_after to a fixed 1.5s
    introduced: at the CLI's default poll interval the inter-sample gap is
    ~1.30s, leaving 1.15 frames of margin, so one dropped frame killed movement
    for several seconds. Deriving the window from the poll interval fixes it at
    every rate -- so this checks every rate, not just the deployed one.
    """
    # MEASURED periods from the README table, not the model's own formula --
    # re-deriving the model here would make the test self-consistent by
    # construction and blind to model error.
    for poll_interval, period in ((0.05, 0.391), (0.1, 0.513), (0.2, 1.299)):
        stale = stale_after_for(poll_interval)
        movement = MovementConfig(enabled=True, source="power",
                                  min_value=0.0, max_value=75.0)
        mapper = Mapper(MappingConfig(movement=movement, stale_after=stale))

        step = 1.0 / FRAME_HZ
        now = 0.0
        next_sample = 0.0
        sample_index = 0
        worst = 1.0
        while now < 120.0:
            if now >= next_sample:
                sample_index += 1
                # Drop one frame in twenty, as a flaky BLE link would.
                if sample_index % 20 != 0:
                    mapper.submit(70.0, 90.0, now=now)
                next_sample += period
            out = mapper.evaluate(now=now)
            if now > 5.0:                     # let the feed warm up first
                worst = min(worst, out.movement_scale)
            now += step

        assert worst > 0.0, (
            f"--poll-interval {poll_interval}: a dropped frame zeroed movement "
            f"(stale_after={stale:.2f}s, period={period:.2f}s)"
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


def test_cadence_is_actually_smoothed():
    """Deleting the EMA entirely used to pass the whole suite.

    Steady-state tests converge, so smoothed and raw agree by the time they
    assert. This drives the output loop's real 60 Hz rate and checks the
    TRANSIENT, which is the only place a filter is visible. Evaluating once a
    second later would not work: the filter is framerate-independent, so over a
    1 s gap it converges ~95% and looks like no filter at all.
    """
    mapper = make_mapper()
    step = 1.0 / FRAME_HZ
    t = 0.0
    while t < 2.0:                       # settle at rest
        mapper.submit(0.0, 0.0, now=t)
        mapper.evaluate(now=t)
        t += step

    start = t
    out = None
    while t < start + 0.1:               # hard step to 80 rpm
        mapper.submit(80.0, 110.0, now=t)
        out = mapper.evaluate(now=t)
        t += step
    assert out.cadence < 40.0, (
        f"cadence reached {out.cadence:.1f} within 0.1s of an 80 rpm step; "
        f"the filter is not doing anything"
    )

    while t < start + 3.0:
        mapper.submit(80.0, 110.0, now=t)
        out = mapper.evaluate(now=t)
        t += step
    assert out.cadence > 70.0, f"filter never converged: {out.cadence:.1f}"


def test_stale_decay_is_a_ramp_not_an_instant_drop():
    """Replacing the decay ramp with an instant zero used to pass everything.

    test_stale_decay_is_framerate_independent passes trivially with no decay at
    all -- instant zero is perfectly framerate-independent.
    """
    tracker = CadenceTracker()
    step = 1.0 / FRAME_HZ
    t = 0.0
    while t < 1.0:                       # converge well inside the stale window
        tracker.submit(80.0, now=t)
        tracker.value(now=t)
        t += step
    last_sample = t

    before = tracker.value(now=last_sample + tracker.stale_after - 0.01)
    assert before > 70.0, f"decayed before going stale: {before:.1f}"

    halfway = tracker.value(
        now=last_sample + tracker.stale_after + tracker.decay_to_zero_over / 2)
    assert 0.0 < halfway < before, (
        f"expected a partial value halfway through the ramp, got {halfway:.1f} "
        f"(0.0 means instant drop, {before:.1f} means no decay at all)"
    )

    gone = tracker.value(
        now=last_sample + tracker.stale_after + tracker.decay_to_zero_over + 0.1)
    assert gone == 0.0, f"ramp never reached zero: {gone:.1f}"


def test_stale_window_is_floored_and_capped():
    assert stale_after_for(0.02) == 1.5, "floor not applied at the fast end"
    assert stale_after_for(2.0) == 3.0, (
        "no cap: a slow poll interval buys seconds of movement from a dead bike"
    )


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


def test_frozen_console_is_treated_as_stopped():
    """Observed on real hardware: the console latched one reading and resent it
    unchanged for 30 seconds while the rider sat still. Frames kept arriving, so
    the staleness check -- which only sees silence -- never fired, and pedalling
    stopped mattering."""
    mapper = make_movement_mapper()
    t = 0.0
    for i in range(20):                      # genuine riding: distance climbs
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
    assert mapper.evaluate(now=t).movement_scale > 0.5
    assert not mapper.is_frozen(now=t)

    frozen_at = t
    for _ in range(20):                      # console repeats itself verbatim
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=157)
    assert t - frozen_at > mapper.config.frozen_after
    assert mapper.is_frozen(now=t)
    assert mapper.evaluate(now=t).movement_scale == 0.0


def test_a_brief_repeat_is_not_a_freeze():
    """The console holds its last reading for ~2s at the end of every pedalling
    stretch. Treating that as a fault would fire on every single stop."""
    mapper = make_movement_mapper()
    t = 0.0
    for i in range(10):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
    for _ in range(4):                       # ~2s of repeats
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=127)
    assert not mapper.is_frozen(now=t)
    assert mapper.evaluate(now=t).movement_scale > 0.5


def test_freeze_detection_needs_distance():
    """Without distance a steady rider is indistinguishable from a stuck
    console, and stopping a real rider is the worse error."""
    mapper = make_movement_mapper()
    t = 0.0
    for _ in range(30):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t)     # no distance supplied
    assert not mapper.is_frozen(now=t)
    assert mapper.evaluate(now=t).movement_scale > 0.5


def test_freeze_guard_can_be_switched_off():
    """FROZEN_AFTER=0 is documented as the kill switch, in config and the
    banner. Nothing tested it, so the comparison could silently invert."""
    movement = MovementConfig(enabled=True, source="power",
                              min_value=0.0, max_value=130.0)
    mapper = Mapper(MappingConfig(movement=movement, frozen_after=0))
    t = 0.0
    for _ in range(40):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=157)   # frozen solid
    assert not mapper.is_frozen(now=t)
    assert mapper.evaluate(now=t).movement_scale > 0.5


def test_an_idle_bike_is_not_frozen():
    """A stopped bike repeats (0, 0, 0) forever. Without the cadence check that
    is a freeze, and it would log and re-log every time you sit still."""
    mapper = make_movement_mapper()
    t = 0.0
    for _ in range(40):
        t += 0.5
        mapper.submit(0.0, 0.0, now=t, distance=0)
    assert not mapper.is_frozen(now=t)


def test_a_dead_link_is_not_reported_as_a_frozen_console():
    """Silence is the staleness check's job. _cadence_raw and the distance flag
    are sticky, so without an explicit guard a plain BLE dropout satisfies every
    freeze condition -- and the one log line that means "frames arrived carrying
    dead data" would fire when no frames arrived at all."""
    mapper = make_movement_mapper()
    t = 0.0
    for i in range(10):
        t += 0.5
        mapper.submit(70.0, 90.0, now=t, distance=100 + i * 3)
    silent = t + mapper.tracker.stale_after + 5.0
    assert mapper.tracker.is_stale(silent)
    assert not mapper.is_frozen(silent), "a dead link was blamed on the console"
    assert mapper.evaluate(now=silent).movement_scale == 0.0   # still zeroed


def test_movement_returns_when_the_console_unfreezes():
    mapper = make_movement_mapper()
    t = 0.0
    for i in range(10):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
    for _ in range(20):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=127)
    assert mapper.evaluate(now=t).movement_scale == 0.0

    for i in range(4):                       # console starts reporting again
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=130 + i * 3)
    assert not mapper.is_frozen(now=t)
    assert mapper.evaluate(now=t).movement_scale > 0.5, "never recovered"


def test_gate_disabled_always_passes():
    mapper = Mapper(MappingConfig(gate=GateConfig(enabled=False)))
    assert mapper.evaluate(now=0.0).gate_open is True


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
