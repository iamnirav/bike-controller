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
    STICK_TAKEOVER,
    CadenceTracker,
    ForcedMovementConfig,
    GateConfig,
    Mapper,
    MappingConfig,
    MovementConfig,
    left_stick,
)
from bike_controller.csc import CrankReading      # noqa: E402

# Two telemetry rates, a slow one and a fast one. Tests default to the SLOWER
# because it is the harder case for the fail-safe, but the fast one is exercised
# explicitly below -- a fail-safe sized in missed frames behaves differently when
# the frame rate triples.
#
# The exact figures came from the poll-rate table in the README, which is now
# flagged there as unverified. That does not weaken these tests: what they need
# is a slow rate and a fast one roughly a factor of three apart, not the true
# rate of any particular poll interval. Do not read them as measurements.
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
    assert out.gate_open is False, "dead feed left the gate OPEN at the fast rate"
    assert out.cadence == 0.0


def test_the_freeze_guard_leaves_movement_alone_at_the_deployed_baseline():
    """A latch must NOT pin a pedalling rider to the floor.

    It used to. But the left stick is only ever the physical stick times this
    scale, so a lying console cannot move anyone by itself -- while flooring a
    rider who is working hard, for as long as the console chooses to stay
    latched, loses fights. The guard gave up the wrong thing.
    """
    mapper = make_movement_mapper(max_value=75.0, floor=0.5)
    t = 0.0
    for i in range(10):
        t += 0.5
        mapper.submit(51.0, 60.0, now=t, distance=100 + i * 3)
    riding = mapper.evaluate(now=t).movement_scale
    assert riding > 0.8

    for _ in range(20):                      # the observed 51rpm/60W latch
        t += 0.5
        mapper.submit(51.0, 60.0, now=t, distance=130)
    assert mapper.is_frozen(now=t)
    assert mapper.evaluate(now=t).movement_scale == riding


def test_movement_scale_zeroes_on_dead_link_at_deployed_rate():
    """Mirrors the gate twin above -- must actually drive the deployed rate."""
    movement = MovementConfig(enabled=True, source="power",
                              min_value=0.0, max_value=75.0)
    mapper = Mapper(MappingConfig(movement=movement))

    _, t = run(mapper, 70.0, seconds=8.0, telemetry_hz=DEPLOYED_HZ)
    assert mapper.evaluate(now=t).movement_scale > 0.9

    out, _ = run(mapper, None, seconds=8.0, t0=t, feed=False,
                 telemetry_hz=DEPLOYED_HZ)
    assert out.movement_scale == mapper.config.movement.floor


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

# Every fail-safe assertion below is written against the CONFIGURED floor, not
# against 0. Asserting 0 only held in a configuration nobody runs: with a
# non-zero deployed floor, eight of these tests failed, so the staleness path and
# the whole freeze guard were unverified in production settings.
#
# The default is in here BY VALUE rather than imported, so that changing it (as
# 0.25 -> 0.5 did, to clear the game's deadzone) is a visible edit to the tests
# and not a silent change of what they cover.
FLOORS = (0.0, 0.25, 0.5)


def make_movement_mapper(**kw) -> Mapper:
    defaults = dict(enabled=True, source="power", min_value=0.0, max_value=130.0,
                    floor=0.0)
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


def test_dead_feed_drops_to_the_baseline():
    """Fail-safe: a stale link must never grant MORE than a working bike.

    At floor 0 that is a hard stop. At a configured baseline it is that
    baseline -- what a live bike gives at 0 W -- so a broken bike is never
    better than a working one.
    """
    for floor in FLOORS:
        mapper = make_movement_mapper(floor=floor)
        mapper.submit(80.0, 120.0, now=1.0)
        assert mapper.evaluate(now=1.0).movement_scale > 0.9

        stale = 1.0 + mapper.tracker.stale_after + 0.1
        out = mapper.evaluate(now=stale)
        assert out.movement_scale == floor, f"floor {floor}: got {out.movement_scale}"
        assert out.sprint is False
        assert out.degraded is True


def test_the_baseline_applies_at_every_effort_including_none():
    """floor is the multiplier you always have; effort scales what is left above
    it. 0 W -> baseline, max -> 1.0."""
    mapper = make_movement_mapper(max_value=75.0, floor=0.25)
    mapper.submit(0.0, 0.0, now=1.0)
    assert abs(mapper.evaluate(now=1.0).movement_scale - 0.25) < 0.001
    mapper.submit(50.0, 37.5, now=2.0)
    assert abs(mapper.evaluate(now=2.0).movement_scale - 0.625) < 0.01
    mapper.submit(70.0, 75.0, now=3.0)
    assert abs(mapper.evaluate(now=3.0).movement_scale - 1.0) < 0.001


def test_a_dead_bike_drops_to_the_baseline_not_to_zero():
    """The baseline is what the CONTROLLER gives you. A bike fault should leave
    you slow, not stranded -- otherwise a dropped link punishes you harder than
    simply not pedalling."""
    mapper = make_movement_mapper(max_value=75.0, floor=0.25)
    mapper.submit(70.0, 75.0, now=1.0)
    assert mapper.evaluate(now=1.0).movement_scale == 1.0
    stale = 1.0 + mapper.tracker.stale_after + 0.1
    assert abs(mapper.evaluate(now=stale).movement_scale - 0.25) < 0.001


def test_zero_baseline_is_still_pedal_or_nothing():
    mapper = make_movement_mapper(max_value=75.0, floor=0.0)
    mapper.submit(0.0, 0.0, now=1.0)
    assert mapper.evaluate(now=1.0).movement_scale == 0.0


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
                              min_value=0.0, max_value=90.0, floor=0.0)
    mapper = Mapper(MappingConfig(movement=movement))
    mapper.submit(45.0, 999.0, now=1.0)
    assert abs(mapper.evaluate(now=1.0).movement_scale - 0.5) < 0.01


def test_a_frozen_console_releases_the_buttons_it_was_holding():
    """The one thing a latch genuinely breaks.

    Bike-driven buttons are written to the pad whatever the rider's hands are
    doing, so a console latched above sprint_at holds sprint down for as long as
    it lies -- controller untouched, nothing the rider can do about it. Movement
    is stick-gated and needs no such rescue; the buttons are not.
    """
    mapper = Mapper(MappingConfig(
        movement=MovementConfig(enabled=True, source="power", min_value=0.0,
                                max_value=130.0, floor=0.0, sprint_at=50.0),
        buttons=[ButtonRule(name="BTN_TR", min_rpm=40.0)],
    ))
    t = 0.0
    for i in range(20):                      # genuine riding: distance climbs
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
        # Every frame, as the output loop does: value() advances the cadence
        # filter, so evaluating only at the end leaves it one step off zero and
        # the threshold rule never opens.
        out = mapper.evaluate(now=t)
    assert out.sprint and "BTN_TR" in out.buttons
    assert not mapper.is_frozen(now=t)

    frozen_at = t
    for _ in range(20):                      # console repeats itself verbatim
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=157)
        out = mapper.evaluate(now=t)
    assert t - frozen_at > mapper.config.frozen_after
    assert mapper.is_frozen(now=t)
    assert not out.sprint, "sprint stayed held on a lying console"
    assert "BTN_TR" not in out.buttons, "threshold button stayed held"


def test_a_frozen_console_does_not_buzz_the_controller():
    """Silence buzzes; a latch is logged and left alone. One is the bike going
    away mid-ride, the other is a fault whose announcement mid-firefight is a
    worse interruption than the fault itself."""
    mapper = make_movement_mapper()
    t = 0.0
    for i in range(10):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
    for _ in range(20):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=127)
    assert mapper.is_frozen(now=t)
    assert not mapper.evaluate(now=t).degraded


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


def test_a_perfectly_steady_rider_is_not_frozen():
    """Distance is what separates a steady rider from a stuck console.

    The twin of test_freeze_detection_needs_distance, which only covers distance
    being ABSENT. This is the case the discriminator exists for: cadence and
    power bit-identical sample after sample -- which at fixed resistance is one
    signal, not two, since the console derives watts from rpm -- while the
    accumulator keeps climbing. Drop distance from the change-detection tuple
    and this rider gets their sprint taken away for pedalling too evenly.
    """
    mapper = make_movement_mapper(sprint_at=50.0)
    t = 0.0
    for i in range(40):                      # 20s of metronomic pedalling
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
        out = mapper.evaluate(now=t)
    assert t > mapper.config.frozen_after * 4
    assert not mapper.is_frozen(now=t), "a steady rider was called a stuck console"
    assert out.sprint, "sprint was released from a live, pedalling rider"


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
    # Still capped at the baseline; the guard changes the LOG, not the outcome.
    assert mapper.evaluate(now=silent).movement_scale == mapper.config.movement.floor


def test_movement_returns_when_the_console_unfreezes():
    mapper = make_movement_mapper()
    t = 0.0
    for i in range(10):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=100 + i * 3)
    for _ in range(20):
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=127)
    assert mapper.is_frozen(now=t)

    for i in range(4):                       # console starts reporting again
        t += 0.5
        mapper.submit(60.0, 70.0, now=t, distance=130 + i * 3)
    assert not mapper.is_frozen(now=t), "never recovered"
    assert mapper.evaluate(now=t).movement_scale > 0.5


def test_gate_disabled_always_passes():
    mapper = Mapper(MappingConfig(gate=GateConfig(enabled=False)))
    assert mapper.evaluate(now=0.0).gate_open is True



def _grace_mapper(grace=5.0, floor=0.5, maximum=100.0):
    """A power-scaled mapper with the blackout grace configured."""
    return Mapper(MappingConfig(
        gate=GateConfig(enabled=False),
        movement=MovementConfig(enabled=True, source="power", min_value=0.0,
                                max_value=maximum, floor=floor,
                                blackout_grace=grace),
    ))


def test_blackout_holds_the_last_live_scale():
    """The console reports zeros for ~5s after you stop and discards any
    pedalling during it. Dropping to the floor there costs a sprint the rider
    cannot start; this holds what they last actually earned."""
    m = _grace_mapper()
    m.submit(cadence_rpm=70, power_w=100, now=0.0, distance=10)
    assert m.evaluate(now=0.0).movement_scale == 1.0

    # Console pauses: live frames, all zeros.
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    assert m.evaluate(now=1.0).movement_scale == 1.0
    m.submit(cadence_rpm=0, power_w=0, now=4.0, distance=0)
    assert m.evaluate(now=4.0).movement_scale == 1.0


def test_grace_expires_back_to_the_floor():
    m = _grace_mapper(grace=5.0, floor=0.5)
    m.submit(cadence_rpm=70, power_w=100, now=0.0, distance=10)
    m.evaluate(now=0.0)
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    m.evaluate(now=1.0)
    # 5s after the blackout BEGAN (t=1.0), not after the last live sample.
    m.submit(cadence_rpm=0, power_w=0, now=6.01, distance=0)
    assert m.evaluate(now=6.01).movement_scale == 0.5


def test_grace_holds_a_partial_scale_not_full_deflection():
    """It holds what was earned, not 1.0 -- stopping while soft-pedalling must
    not hand out more movement than the pedalling did."""
    m = _grace_mapper(floor=0.5, maximum=100.0)
    m.submit(cadence_rpm=40, power_w=50, now=0.0, distance=10)
    earned = m.evaluate(now=0.0).movement_scale
    assert earned == 0.75, earned          # floor 0.5 + half the headroom
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    assert m.evaluate(now=1.0).movement_scale == 0.75


def test_resumed_telemetry_ends_the_grace_immediately():
    m = _grace_mapper()
    m.submit(cadence_rpm=70, power_w=100, now=0.0, distance=10)
    m.evaluate(now=0.0)
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    m.evaluate(now=1.0)
    # Console comes back reporting a genuinely low effort: that must win over
    # the held value straight away, not linger for the rest of the window.
    m.submit(cadence_rpm=10, power_w=0, now=2.0, distance=12)
    assert m.evaluate(now=2.0).movement_scale == 0.5


def test_a_second_blackout_rearms_the_grace():
    m = _grace_mapper()
    m.submit(cadence_rpm=70, power_w=100, now=0.0, distance=10)
    m.evaluate(now=0.0)
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    m.evaluate(now=1.0)
    m.submit(cadence_rpm=70, power_w=100, now=8.0, distance=20)
    m.evaluate(now=8.0)
    m.submit(cadence_rpm=0, power_w=0, now=9.0, distance=0)
    assert m.evaluate(now=9.0).movement_scale == 1.0


def test_an_untouched_bike_gets_the_floor_not_a_held_zero():
    """At startup everything reads zero. With no live scale ever measured,
    holding one would serve 0.0 for five seconds -- worse than the floor it
    replaces, and on the very path where the rider has done nothing wrong."""
    m = _grace_mapper(floor=0.5)
    m.submit(cadence_rpm=0, power_w=0, now=0.0, distance=0)
    assert m.evaluate(now=0.0).movement_scale == 0.5
    m.submit(cadence_rpm=0, power_w=0, now=3.0, distance=0)
    assert m.evaluate(now=3.0).movement_scale == 0.5


def test_a_dead_feed_never_collects_grace():
    """The invariant that a broken bike grants no more than a working one.
    Silence is not a blackout, and must not be paid for like one."""
    m = _grace_mapper(floor=0.5)
    m.submit(cadence_rpm=70, power_w=100, now=0.0, distance=10)
    assert m.evaluate(now=0.0).movement_scale == 1.0
    # No submit() at all: the link is gone, so the tracker goes stale.
    out = m.evaluate(now=30.0)
    assert out.movement_scale == 0.5
    assert out.degraded


def test_a_frozen_console_is_not_a_blackout():
    """A latch reports non-zero readings, so it never looks blacked out and the
    grace clock never starts. Worth pinning: the two now share a code path, and
    a latched reading of exactly (0, 0) would be a blackout by any test -- which
    is why is_frozen requires a non-zero cadence in the first place."""
    m = _grace_mapper(floor=0.5)
    for i in range(12):
        # Bit-identical readings, arriving steadily, for well over frozen_after.
        m.submit(cadence_rpm=51, power_w=60, now=float(i), distance=348)
    out = m.evaluate(now=11.0)
    assert m.is_frozen(now=11.0)
    # Movement is left alone, and no hold was armed -- there was no blackout.
    assert out.movement_scale > 0.5
    assert m._blackout_since is None


def test_grace_of_zero_restores_the_plain_floor():
    m = _grace_mapper(grace=0.0, floor=0.5)
    m.submit(cadence_rpm=70, power_w=100, now=0.0, distance=10)
    m.evaluate(now=0.0)
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    assert m.evaluate(now=1.0).movement_scale == 0.5


def test_sprint_is_released_during_the_grace():
    """Holding the scale keeps the stick where it was; latching a BUTTON on no
    evidence is a bigger promise than this feature is making."""
    m = Mapper(MappingConfig(
        gate=GateConfig(enabled=False),
        movement=MovementConfig(enabled=True, source="power", min_value=0.0,
                                max_value=100.0, floor=0.5, blackout_grace=5.0,
                                sprint_at=80.0),
    ))
    m.submit(cadence_rpm=80, power_w=100, now=0.0, distance=10)
    assert m.evaluate(now=0.0).sprint
    m.submit(cadence_rpm=0, power_w=0, now=1.0, distance=0)
    out = m.evaluate(now=1.0)
    assert out.movement_scale == 1.0
    assert not out.sprint


# --- Pedalling forces movement --------------------------------------------
#
# The console's stop sequence, as measured (see tools/idle_probe.py): a ~2s
# FREEZE repeating the last cadence/power with distance stalled, then a ~4.5s
# PAUSE of all zeros. These drive that shape at the deployed telemetry rate.

def forced_mapper(**movement_kw) -> Mapper:
    movement = MovementConfig(enabled=True, max_value=75.0, floor=0.5,
                              **movement_kw)
    return Mapper(MappingConfig(movement=movement,
                                forced_movement=ForcedMovementConfig(enabled=True),
                                stale_after=1.5))


def ride(mapper, seconds, t0=0.0, distance0=100, cadence=60.0, power=70.0,
         step=6, hz=DEPLOYED_HZ):
    """Pedal: distance climbs by `step` per sample. Returns (out, t, distance)."""
    t, d, out = t0, distance0, None
    while t < t0 + seconds:
        d += step
        mapper.submit(cadence, power, now=t, distance=d)
        out = mapper.evaluate(now=t)
        t += 1.0 / hz
    return out, t, d


def hold(mapper, seconds, t0, cadence, power, distance, hz=DEPLOYED_HZ):
    """Telemetry keeps arriving, but every sample is identical."""
    t, trace = t0, []
    while t < t0 + seconds:
        mapper.submit(cadence, power, now=t, distance=distance)
        trace.append((t - t0, mapper.evaluate(now=t)))
        t += 1.0 / hz
    return trace, t


def test_forced_movement_pushes_forward_while_pedalling():
    mapper = forced_mapper()
    out, _, _ = ride(mapper, 5.0)
    assert out.forced > 0.5, out.forced
    assert out.forced == out.movement_scale


def test_forced_movement_off_by_default():
    mapper = Mapper(MappingConfig(movement=MovementConfig(enabled=True)))
    t, d = 0.0, 100
    for _ in range(10):
        d += 6
        mapper.submit(60.0, 70.0, now=t, distance=d)
        out = mapper.evaluate(now=t)
        t += 0.4
    assert out.forced == 0.0


def test_forced_movement_stops_within_release_during_the_freeze():
    """The whole point of watching distance: the freeze keeps reporting your
    cadence for ~2s, but forcing must let go after ~release_after."""
    mapper = forced_mapper()
    _, t, d = ride(mapper, 5.0)
    trace, _ = hold(mapper, 2.0, t, 60.0, 70.0, d)
    released = [dt for dt, out in trace if out.forced == 0.0]
    assert released, "forcing held through the whole freeze"
    assert released[0] <= mapper.config.forced_movement.release_after + 0.5, released[0]
    # and once released, it stays released for the rest of the freeze
    assert all(out.forced == 0.0 for dt, out in trace if dt >= released[0])


def test_forced_movement_off_through_the_pause_and_back_on_after():
    mapper = forced_mapper()
    _, t, d = ride(mapper, 5.0)
    _, t = hold(mapper, 2.0, t, 60.0, 70.0, d)
    trace, t = hold(mapper, 4.5, t, 0.0, 0.0, 0)
    assert all(out.forced == 0.0 for _, out in trace)
    out, _, _ = ride(mapper, 1.0, t0=t, distance0=d + 30)
    assert out.forced > 0.0


def test_latched_console_cannot_force_movement():
    """The 30s freeze seen on real hardware repeats distance too."""
    mapper = forced_mapper()
    _, t, d = ride(mapper, 5.0)
    trace, _ = hold(mapper, 30.0, t, 51.0, 60.0, d)
    assert all(out.forced == 0.0 for dt, out in trace if dt > 2.0)


def test_silent_link_cannot_force_movement():
    mapper = forced_mapper()
    _, t, _ = ride(mapper, 5.0)
    out = mapper.evaluate(now=t + 2.0)
    assert out.forced == 0.0


def test_no_distance_never_forces_movement():
    mapper = forced_mapper()
    t = 0.0
    for _ in range(20):
        mapper.submit(60.0, 70.0, now=t, distance=None)
        out = mapper.evaluate(now=t)
        t += 0.4
    assert out.forced == 0.0


def test_distance_wrap_counts_as_pedalling():
    mapper = forced_mapper()
    mapper.submit(60.0, 70.0, now=0.0, distance=65533)
    mapper.submit(60.0, 70.0, now=0.4, distance=3)
    assert mapper.evaluate(now=0.4).forced > 0.0


def test_reset_to_zero_is_not_pedalling():
    mapper = forced_mapper()
    mapper.submit(60.0, 70.0, now=0.0, distance=500)
    mapper.submit(60.0, 70.0, now=0.4, distance=0)
    assert mapper.evaluate(now=0.4).forced == 0.0


def test_forced_movement_scales_with_effort():
    easy = forced_mapper()
    hard = forced_mapper()
    easy_out, _, _ = ride(easy, 3.0, power=10.0)
    hard_out, _, _ = ride(hard, 3.0, power=75.0)
    assert easy_out.forced < hard_out.forced == 1.0


def test_forced_movement_can_be_switched_live():
    mapper = forced_mapper()
    _, t, d = ride(mapper, 3.0)
    mapper.config.forced_movement.enabled = False
    out, _, _ = ride(mapper, 1.0, t0=t, distance0=d)
    assert out.forced == 0.0


def test_left_stick_centred_is_forced_forward():
    assert left_stick(0, 0, 0.8, 0.8) == (0, -26214)
    # drift below the takeover threshold is still "centred"
    assert left_stick(2000, -1500, 0.8, 0.8) == (0, -26214)


def test_left_stick_deflected_overrides_forcing():
    full = 32767
    x = int(STICK_TAKEOVER * full) + 100
    assert left_stick(x, 0, 0.5, 0.9) == (int(x * 0.5), 0)
    # pulling back is honoured, not summed with forward
    assert left_stick(0, 30000, 0.5, 0.9) == (0, 15000)


def test_left_stick_unforced_is_plain_scaling():
    assert left_stick(0, 0, 0.5, 0.0) == (0, 0)
    assert left_stick(1000, -2000, 0.5, 0.0) == (500, -1000)


# --- The crank sensor -------------------------------------------------------
#
# The console's stop sequence again -- ~2s freeze, then ~5s of zeros -- but now
# with a Bluetooth crank sensor reporting alongside it. These pin the promise
# made to the rider: with the sensor, stopping is a stop and pedalling through
# the pause is pedalling; without it, nothing changes.

class Crank:
    """A crank sensor attached to `mapper`, turning when told to."""

    def __init__(self, mapper, connected=True):
        self.mapper, self.revs, self.ticks = mapper, 0, 0
        if connected:
            mapper.crank.link_up()
        mapper.crank.submit(CrankReading(0, 0), now=-100.0)   # baseline

    def turns(self, start, end, rpm=60.0):
        """Revolutions at `rpm` from start (exclusive) to end (inclusive)."""
        period = 60.0 / rpm
        t = start + period
        while t <= end + 1e-9:
            self.revs += 1
            self.ticks += round(period * 1024)
            self.mapper.crank.submit(
                CrankReading(self.revs & 0xFFFF, self.ticks & 0xFFFF), now=t)
            t += period


def sensor_mapper(enabled=True, **movement_kw):
    m = forced_mapper(**movement_kw)
    m.config.crank_sensor.enabled = enabled
    return m


def stop_sequence(m, crank, pedal_through=False, rpm=60.0):
    """Ride 5s, freeze 2s, then pause. Returns (t_pause_start, distance)."""
    crank.turns(-1.0, 5.0, rpm)
    _, t, d = ride(m, 5.0)
    trace, t = hold(m, 2.0, t, 60.0, 70.0, d)
    m.submit(0, 0, now=t, distance=0)
    return t, d, trace


def pause(m, t0, seconds):
    """Zeros at the deployed rate; returns the outputs with their offsets."""
    t, trace = t0, []
    while t < t0 + seconds:
        m.submit(0, 0, now=t, distance=0)
        trace.append((t - t0, m.evaluate(now=t)))
        t += 1.0 / DEPLOYED_HZ
    return trace


def test_without_a_sensor_the_pause_still_holds():
    """Sensor dial off: exactly the old behaviour, grace hold and all."""
    m = sensor_mapper(enabled=False)
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    out = pause(m, t, 3.0)[-1][1]
    assert out.movement_scale > 0.9, out.movement_scale
    assert not out.from_sensor


def test_a_disconnected_sensor_changes_nothing():
    m = sensor_mapper()
    crank = Crank(m, connected=False)
    t, _, _ = stop_sequence(m, crank)
    out = pause(m, t, 3.0)[-1][1]
    assert out.movement_scale > 0.9
    assert not out.from_sensor
    assert out.crank_rpm is None


def test_stopping_drops_to_the_floor_with_no_hold():
    m = sensor_mapper()
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    for _, out in pause(m, t, 4.0):
        assert out.movement_scale == 0.5, out.movement_scale
        assert out.forced == 0.0
        assert out.from_sensor


def test_the_freeze_is_cut_short_once_the_sensor_agrees():
    """Console repeats 60 rpm / 70 W for 2s; distance and the sensor both say
    stopped. The stop lands inside the freeze instead of after it.

    Only once the sensor's hold runs out, which is a period plus a
    notification interval: at 120 rpm that fits inside the 2s freeze, at
    60 rpm it does not (the hold has to cover a slow rider's next stroke
    arriving a notification late, and a stop is the cheaper error to delay)."""
    m = sensor_mapper()
    crank = Crank(m)
    crank.turns(-1.0, 5.0, rpm=120.0)
    _, t, d = ride(m, 5.0)
    trace, _ = hold(m, 2.0, t, 60.0, 70.0, d)
    early = [out.movement_scale for dt, out in trace if dt < 0.5]
    late = [out.movement_scale for dt, out in trace if dt > 1.8]
    assert all(s > 0.9 for s in early), early
    assert late and all(s == 0.5 for s in late), late


def test_a_sensor_that_still_sees_turns_does_not_cut_the_freeze():
    """Sensor turning, distance stalled: one witness is not enough."""
    m = sensor_mapper()
    crank = Crank(m)
    crank.turns(-1.0, 7.0)
    _, t, d = ride(m, 5.0)
    trace, _ = hold(m, 2.0, t, 60.0, 70.0, d)
    assert all(out.movement_scale > 0.9 for _, out in trace)


def test_pedalling_through_the_pause_keeps_you_moving():
    m = sensor_mapper()
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    # Resume 0.5s into the pause at the same cadence.
    crank.turns(t - 0.5, t + 5.0)
    trace = pause(m, t, 4.5)
    later = [out for dt, out in trace if dt > 2.0]
    assert later
    for out in later:
        assert out.from_sensor
        assert out.forced > 0.9, out.forced
        assert abs(out.power - 70.0) < 3, out.power      # estimated, not 0
        assert abs(out.crank_rpm - 60) < 1


def test_effort_estimate_scales_with_sensor_cadence():
    m = sensor_mapper()
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    crank.turns(t - 0.5, t + 5.0, rpm=30.0)
    out = pause(m, t, 4.5)[-1][1]
    assert abs(out.power - 35.0) < 3, out.power
    assert abs(out.movement_scale - (0.5 + 0.5 * 35 / 75)) < 0.03


def test_turns_from_before_the_pause_do_not_restart_you():
    """The sensor's hold outlives the last revolution by up to 3s. Turns from
    BEFORE the pause must not walk a rider who stopped during the freeze."""
    m = sensor_mapper()
    crank = Crank(m)
    crank.turns(-1.0, 5.0, rpm=30.0)        # slow: long hold
    _, t, d = ride(m, 5.0)
    _, t = hold(m, 1.2, t, 60.0, 70.0, d)   # a short freeze
    out = pause(m, t, 1.0)[0][1]
    assert m.crank.turning(t)               # the sensor alone would say yes
    assert out.forced == 0.0
    assert out.movement_scale == 0.5


def test_first_turn_after_a_rest_forces_movement_at_the_held_scale():
    m = sensor_mapper()
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    # A long gap, then one revolution: turning, rate unknown.
    crank.revs += 1
    crank.ticks += 3 * 1024
    m.crank.submit(CrankReading(crank.revs, crank.ticks), now=t + 3.0)
    out = pause(m, t + 3.0, 0.2)[0][1]
    assert m.crank.rpm(t + 3.0) is None
    assert out.forced > 0.9
    assert out.movement_scale > 0.9         # the last earned scale


def test_the_gauge_shows_the_effort_a_hold_is_standing_on():
    """While the sensor proves pedalling but not yet a rate, movement holds the
    last earned scale -- and the gauge must show that effort, not the console's
    zero, or the rider sees 0 W while still walking."""
    m = sensor_mapper()
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    crank.revs += 1
    crank.ticks += 3 * 1024
    m.crank.submit(CrankReading(crank.revs, crank.ticks), now=t + 3.0)
    out = pause(m, t + 3.0, 0.2)[0][1]
    assert out.from_sensor
    assert out.power == 70.0, out.power


def drive(m, t0, t1, console, turn_times, report_every=1.0):
    """The bike and a 1 Hz sensor together, in time order.

    console(t) -> (cadence, power, distance) at the deployed rate. The sensor
    reports every `report_every` from t0 with the count of turn_times so far,
    as a real one does: a turn reaches us at the next report, and between
    turns it repeats itself. Returns [(t, out)] at each console sample.
    """
    turns = sorted(turn_times)
    events = []
    t = t0
    while t < t1:
        events.append((t, "bike"))
        t += 1.0 / DEPLOYED_HZ
    t = t0
    while t < t1:
        events.append((t, "sensor"))
        t += report_every
    events.sort(key=lambda e: (e[0], e[1] == "bike"))
    trace = []
    for t, kind in events:
        if kind == "sensor":
            done = [x for x in turns if x <= t]
            last = done[-1] if done else -100.0
            m.crank.submit(CrankReading(len(done) & 0xFFFF,
                                        round(last * 1024) & 0xFFFF), now=t)
        else:
            cadence, power, distance = console(t)
            m.submit(cadence, power, now=t, distance=distance)
            trace.append((t, m.evaluate(now=t)))
    return trace


def every(start, end, rpm):
    period = 60.0 / rpm
    n = int((end - start) / period + 1e-9)
    return [start + period * (i + 1) for i in range(n)]


def pedalling(t):
    return 75.0, 90.0, 100 + int(t * 12)


def test_the_sensor_ends_movement_while_the_console_lags():
    """From a real ride: the console kept reporting pedalling, distance and
    all, for 2-4s after the cranks stopped. The sensor's stop wins."""
    m = sensor_mapper()
    m.crank.link_up()
    trace = drive(m, 0.0, 10.0, pedalling, every(0.0, 5.0, rpm=75))
    before = [o for t, o in trace if 2.0 < t < 5.0]
    after = [o for t, o in trace if t > 7.6]
    assert before and all(o.forced > 0.5 for o in before)
    assert after and all(o.forced == 0.0 and o.movement_scale == 0.5
                         for o in after), [(round(t, 2), o.forced)
                                           for t, o in trace if t > 5]


def test_a_sensor_that_never_turns_cannot_stop_a_pedalling_rider():
    """Fallen off the crank, or on another bike: it talks, it never turns.
    The console's pedalling must stand."""
    m = sensor_mapper()
    m.crank.link_up()
    trace = drive(m, 0.0, 30.0, pedalling, [])
    assert all(o.forced > 0.5 for t, o in trace if t > 1.5)


def test_a_sensor_whose_turns_stopped_long_ago_cannot_stop_a_rider():
    m = sensor_mapper()
    m.crank.link_up()
    trace = drive(m, 0.0, 30.0, pedalling, every(0.0, 3.0, rpm=60))
    late = [o for t, o in trace if t > 3.0 + Mapper.SENSOR_TRUST + 0.5]
    assert late and all(o.forced > 0.5 for o in late)


def test_a_silent_sensor_is_not_read_as_a_stop():
    m = sensor_mapper()
    m.crank.link_up()
    drive(m, 0.0, 5.0, pedalling, every(0.0, 5.0, rpm=75))
    # The sensor says nothing more; the console carries on.
    out, t, _ = ride(m, 5.0, t0=5.0, distance0=200)
    assert out.forced > 0.5, out


def test_a_restart_the_console_saw_first_is_not_overruled():
    """Coming back from the pause, the console can report pedalling before
    the sensor's first revolution reaches us. The sensor's 'stopped' is about
    the PREVIOUS run and must not pin the rider to the floor."""
    m = sensor_mapper()
    m.crank.link_up()

    def console(t):
        if t < 5.0:
            return pedalling(t)
        if t < 10.0:
            return 0.0, 0.0, 0                      # stopped, then the pause
        return pedalling(t)                         # back, before the sensor

    trace = drive(m, 0.0, 13.0, console, every(0.0, 4.0, rpm=75))
    back = [o for t, o in trace if 10.5 < t < 13.0]
    assert back and all(o.forced > 0.5 for o in back), back


def test_a_restart_inside_the_freeze_carries_into_the_pause():
    """Stop, then pedal again while the console is still frozen: you are
    pedalling before its zeros arrive, and must not drop to the floor at
    them while waiting for the next revolution to be reported."""
    m = sensor_mapper()
    m.crank.link_up()

    def console(t):
        if t < 5.0:
            return pedalling(t)
        if t < 7.0:
            return 75.0, 90.0, 100 + int(5.0 * 12)  # the freeze
        return 0.0, 0.0, 0                          # the pause

    turns = every(0.0, 3.0, rpm=60) + every(5.6, 9.0, rpm=60)
    trace = drive(m, 0.0, 9.0, console, turns)
    pause_out = [o for t, o in trace if 7.0 <= t < 9.0]
    assert pause_out and all(o.from_sensor and o.movement_scale > 0.5
                             and o.forced > 0.5 for o in pause_out), \
        [(round(t, 2), o.movement_scale, o.forced) for t, o in trace if t > 5]


def test_sprint_follows_the_estimate_through_the_pause():
    m = sensor_mapper(sprint_at=60.0)
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    crank.turns(t - 0.5, t + 5.0, rpm=60.0)
    assert pause(m, t, 4.0)[-1][1].sprint


def test_cadence_scaled_movement_uses_the_sensor_rpm():
    m = sensor_mapper(source="cadence")
    m.config.movement.max_value = 90.0
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    crank.turns(t - 0.5, t + 5.0, rpm=45.0)
    out = pause(m, t, 4.0)[-1][1]
    assert abs(out.movement_scale - 0.75) < 0.02, out.movement_scale


def test_a_dead_console_is_not_overruled_by_the_sensor():
    """Silence is the console's fault path and stays the floor: the sensor
    stands in for a console that is pausing, not one that is gone."""
    m = sensor_mapper()
    crank = Crank(m)
    crank.turns(-1.0, 30.0)
    ride(m, 5.0)
    out = m.evaluate(now=30.0)
    assert out.degraded
    assert out.movement_scale == 0.5
    assert out.forced == 0.0


def test_the_sensor_never_forces_movement_with_that_mode_off():
    """The sensor is evidence for "pedalling forces movement", not a switch for
    it. With the mode off it only sets the scale on the physical stick."""
    m = sensor_mapper()
    m.config.forced_movement.enabled = False
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    crank.turns(t - 0.5, t + 5.0)
    for _, out in pause(m, t, 4.5):
        assert out.forced == 0.0
    assert out.from_sensor and out.movement_scale > 0.9
    # And the switch is live in both directions.
    m.config.forced_movement.enabled = True
    assert pause(m, t + 4.5, 0.5)[-1][1].forced > 0.9


def test_switching_the_sensor_off_mid_pause_restores_the_hold():
    m = sensor_mapper()
    crank = Crank(m)
    t, _, _ = stop_sequence(m, crank)
    assert pause(m, t, 1.0)[-1][1].movement_scale == 0.5
    m.config.crank_sensor.enabled = False
    assert pause(m, t + 1.0, 1.0)[-1][1].movement_scale > 0.9


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
