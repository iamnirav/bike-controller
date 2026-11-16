"""Turn bike telemetry into gamepad input.

Deliberately free of any BLE or uinput dependency: this is pure logic over
numbers, so it can be unit-tested without hardware and reused unchanged if the
output layer switches from uinput to USB HID gadget mode.

Mapping modes, independently toggleable:

  movement  the left stick's deflection scales with effort -- THE headline
            feature and the only one enabled in production
  sprint    a button is held above an effort threshold (part of movement)
  gate      the real controller only passes through while you are pedalling
  axis      cadence drives an analog axis (throttle, stick, whatever)
  buttons   cadence thresholds fire discrete button presses

The console reports at only a couple of Hz, so raw cadence is far too steppy to
drive an axis directly. CadenceTracker smooths it and, critically, decays toward
zero when samples stop arriving -- otherwise a dropped BLE link would leave the
gate stuck open with the game happily accepting input from a stationary bike.
"""

from __future__ import annotations

import math
import time
from typing import Literal
from dataclasses import dataclass, field

from bike_controller.csc import CrankTracker


@dataclass
class CadenceTracker:
    """Smooths a slow, jittery cadence signal into something usable per-frame.

    `smoothing` is the EMA weight applied per second of elapsed time, so the
    filter behaves the same regardless of how fast the caller polls it.
    """

    smoothing_per_second: float = 3.0
    # How long without a sample before the feed counts as dead. This is a
    # function of the telemetry rate, not a free constant: too large and the
    # fail-safe is lax, too small and a single dropped BLE frame kills movement
    # mid-ride. Callers should derive it -- see stale_after_for() -- rather than
    # accept this default, which only suits the fastest poll rate.
    stale_after: float = 1.5
    decay_to_zero_over: float = 2.0

    _value: float = 0.0
    _last_sample: float = 0.0
    # Time of the last SAMPLE, used for staleness.
    _last_update: float = field(default_factory=time.monotonic)
    # Time of the last EVALUATION, used for the filter step. These must be
    # tracked separately: deriving the filter's dt from the sample time makes
    # alpha collapse to zero whenever a sample and an evaluation share a
    # timestamp, and the filter then never converges.
    _last_eval: float | None = None
    _seen: bool = False
    # Value captured when the feed first went stale, so the fade below is a
    # linear ramp in WALL TIME rather than a per-call compounding multiply.
    # The old form made fail-safe timing depend on the caller's frame rate.
    _stale_from: float | None = None

    def submit(self, cadence_rpm: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._last_sample = cadence_rpm
        self._last_update = now
        self._stale_from = None
        self._seen = True

    def is_stale(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._seen and (now - self._last_update) > self.stale_after

    def value(self, now: float | None = None) -> float:
        """Current smoothed cadence. Call this every output frame."""
        now = time.monotonic() if now is None else now
        if not self._seen:
            return 0.0

        dt = 0.0 if self._last_eval is None else max(0.0, now - self._last_eval)
        self._last_eval = now

        age = now - self._last_update
        if age > self.stale_after:
            # No fresh data. Ramp to zero rather than holding a stale value --
            # a stuck-open gate is the dangerous failure, not a false stop.
            if self._stale_from is None:
                self._stale_from = self._value
            overdue = age - self.stale_after
            fade = max(0.0, 1.0 - overdue / self.decay_to_zero_over)
            self._value = self._stale_from * fade
            return self._value
        self._stale_from = None

        # Exponential approach to the latest sample, framerate-independent.
        alpha = 1.0 - math.exp(-self.smoothing_per_second * dt)
        self._value += (self._last_sample - self._value) * min(1.0, alpha)
        return self._value


@dataclass
class GateConfig:
    """Hysteretic gate: opens above `open_rpm`, closes below `close_rpm`.

    Two separate thresholds stop the gate chattering when you hover right at the
    boundary. `grace_seconds` keeps it open briefly after you drop below, so a
    single slow pedal stroke does not kill your input mid-fight.
    """

    enabled: bool = True
    open_rpm: float = 40.0
    close_rpm: float = 25.0
    grace_seconds: float = 1.5


@dataclass
class AxisConfig:
    """Maps cadence onto a normalised 0..1 axis value."""

    enabled: bool = True
    min_rpm: float = 30.0
    max_rpm: float = 90.0


@dataclass
class MovementConfig:
    """Scale the left stick's deflection by how hard you are working.

    Deliberately NOT smoothed. The raw value is passed straight through so the
    real feel can be judged before deciding whether a filter is wanted at all.

    With `min_value = 0` AND `floor = 0`, the game's own deadzone becomes the
    lower threshold -- you must work hard enough to clear it before you move.
    That is typically 12.5% (Unity default) to 24% (XInput recommended) of full
    deflection, and it self-calibrates to whatever game you are in.

    A non-zero `floor` deliberately defeats that: it starts you above the
    deadzone at zero effort, which is the whole point of a baseline. The two
    settings are alternatives, not companions.
    """

    enabled: bool = False
    source: Literal["power", "cadence"] = "power"
    min_value: float = 0.0
    max_value: float = 130.0
    # The BASELINE: the multiplier you always have, at any effort including
    # none. 0.5 means the controller alone gives you a slow walk and the bike
    # buys the rest, so 0 W maps to 0.5 and max_value maps to 1.0.
    #
    # This is what makes bike-side faults degrade instead of strand you: the
    # console's restart delay or a dropped link leave you moving slowly rather
    # than stuck. 0.0 restores strict pedal-or-nothing. (A frozen console is
    # NOT in that list -- see evaluate(); it leaves movement alone.)
    #
    # Why 0.5 and not something smaller: the number has to clear the DOWNSTREAM
    # deadzone, and the game's is the one that counts. Games apply a radial
    # deadzone and rescale what is left, so a floor just above it arrives as
    # nearly nothing -- the first default, 0.25, sat one point above the XInput
    # recommended 0.2395 and produced ~1.4% of movement. Worse, the movement
    # stick is commonly deadzoned far harder than the aim stick: measured in
    # Helldivers 2 with its own deadzone slider off, the RIGHT stick registered
    # at 0.1 while the LEFT stick needed roughly half deflection to walk at all.
    # Tune this against a game, not against the arithmetic.
    floor: float = 0.5
    # Seconds to hold the last live scale when the console blacks out.
    #
    # Stopping pedalling makes the console PAUSE: it reports cadence 0, power 0
    # and distance 0 for about five seconds, and pedalling during that window is
    # measured but discarded (the distance accumulator advances 27-43 counts
    # across it while telemetry reads zero). It is not a fault and there is no
    # way to switch it off -- the manual documents it, and re-init, keep-alives
    # and resistance commands were all tried and do nothing.
    #
    # So the numbers say nothing for five seconds, and the honest options are to
    # assume the rider stopped or to assume they are still going. Assuming they
    # stopped costs a sprint they cannot start; assuming they are going costs
    # movement they did not ask for. This holds the last live value instead --
    # which is safe because movement_scale multiplies the PHYSICAL stick, so a
    # centred stick still gives zero. The failure mode is not a runaway rig, it
    # is only "I was holding forward while resting".
    #
    # 5.0 matches the console's own lockout, so the hold ends about when real
    # telemetry returns. 0 disables it and restores the plain floor.
    blackout_grace: float = 5.0
    sprint_at: float | None = None         # same units as `source`
    # Sprint releases below sprint_at * this. Without hysteresis, effort
    # fluctuating around the threshold makes the sprint button chatter on and
    # off several times a second.
    sprint_release_ratio: float = 0.92


@dataclass
class ForcedMovementConfig:
    """Pedal to walk forward: the bike pushes the left stick while you ride.

    Off by default, because it changes what the bike is allowed to do. Every
    other movement feature multiplies the PHYSICAL stick, so a centred stick is
    zero whatever the bike says. This is the one place the bike alone moves the
    character, so its evidence has to be stronger than "the console reported a
    number".

    That evidence is the distance accumulator ADVANCING, not cadence:

      - When you stop, the console repeats your last cadence and power for ~2s
        (the freeze) but distance stops climbing at once. Cadence would carry
        you two seconds further than you meant to go; distance stops you in
        about one sample.
      - A latched console (the 30-second freeze seen on real hardware) repeats
        distance too, so it cannot force anyone anywhere.
      - The ~4.5s pause after a stop reports distance 0, so forcing is off for
        all of it. Pedalling during the pause is thrown away by the console,
        and nothing here can recover it: a stop longer than ~2s costs a slow
        restart. The physical stick still walks you at the floor meanwhile.

    A console that reports no distance at all never forces movement.
    """

    enabled: bool = False
    # Seconds since distance last advanced before you count as stopped. Must
    # cover the gap between two telemetry samples with margin, or slow pedalling
    # stutters; much more and the stop stops feeling like a stop. ~1-2.5 Hz
    # telemetry is 0.4-0.8s between samples.
    release_after: float = 1.0


@dataclass
class CrankSensorConfig:
    """A Bluetooth crank cadence sensor as a second witness to pedalling.

    The console lies twice every time you stop: for ~2s it repeats your last
    cadence and power (the freeze), then for ~5s it reports zeros and discards
    anything you pedal (the pause). Without another source, the bridge can only
    guess through both -- which is what MovementConfig.blackout_grace does.

    With a sensor connected and this on, it stops guessing:

      - Pause, sensor sees no turns: you stopped. Straight to the floor, no hold.
      - Pause, sensor sees turns: you are pedalling through it. Movement,
        sprint and forced movement follow an effort estimated from the sensor.
      - Freeze, sensor sees no turns and distance has stalled: you stopped.
        The floor at once, rather than two more seconds of your old speed.

    Everything else -- live telemetry, a dead console link -- is the console's
    call as before. A sensor that is off, missing or disconnected changes
    nothing at all: the grace hold comes back as the fallback.
    """

    enabled: bool = False


@dataclass
class ButtonRule:
    """Holds a button while cadence is at or above min_rpm."""

    name: str
    min_rpm: float


def stale_after_for(poll_interval: float, packets_per_cycle: int = 5,
                    frames_of_margin: float = 4.0) -> float:
    """How long to wait before declaring the telemetry feed dead.

    The console answers one poll per request and the poll cycle is
    `packets_per_cycle` writes, so the inter-sample period is roughly
    `packets * (interval + round_trip)`. The 0.03 s round trip is measured: it
    predicts 0.39 s at --poll-interval 0.05 and 1.15 s at 0.2, against 0.39 s
    and 1.30 s observed.

    Sizing in FRAMES rather than seconds is the point. A hand-set constant has
    to agree with a poll interval nobody remembers to check, and when the poll
    rate tripled the constant did not follow.
    """
    period = packets_per_cycle * (poll_interval + 0.03)
    # Clamped at both ends. The floor suits the fastest poll rate; the cap
    # matters more -- without it a typo'd --poll-interval silently buys tens of
    # seconds of full-deflection movement from a dead bike, which is precisely
    # what this window exists to prevent. Past a few seconds the right answer
    # stops being "scale with the poll rate" and becomes "this feed is too slow
    # to drive movement safely at all".
    return min(3.0, max(1.5, frames_of_margin * period))


@dataclass
class MappingConfig:
    gate: GateConfig = field(default_factory=GateConfig)
    axis: AxisConfig = field(default_factory=AxisConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)
    forced_movement: ForcedMovementConfig = field(
        default_factory=ForcedMovementConfig)
    crank_sensor: CrankSensorConfig = field(default_factory=CrankSensorConfig)
    buttons: list[ButtonRule] = field(default_factory=list)
    # None keeps CadenceTracker's default; set it from stale_after_for().
    stale_after: float | None = None
    # Seconds of bit-identical telemetry before the console is treated as
    # frozen. Observed on real hardware: it latched cadence 51 / power 60 /
    # distance 348 and resent it unchanged for 30 seconds. Frames kept
    # arriving, so the staleness check -- which only sees SILENCE -- never
    # fired.
    #
    # What tripping this does is narrow on purpose: it releases the bike-driven
    # buttons and logs, and leaves movement alone. See evaluate() for why.
    #
    # A short freeze is normal: the console holds its last reading for ~2s at
    # the end of every pedalling stretch before zeroing. 4s clears that with
    # margin and is far below the 30s failure.
    frozen_after: float = 4.0


@dataclass
class MappingOutput:
    gate_open: bool = True
    axis: float = 0.0
    buttons: set[str] = field(default_factory=set)
    cadence: float = 0.0
    # Multiplier applied to the left stick, 0.0-1.0. 1.0 when movement scaling
    # is disabled, so callers can multiply unconditionally.
    movement_scale: float = 1.0
    sprint: bool = False
    at_max: bool = False
    # Telemetry has gone SILENT. Deliberately not set for a frozen console,
    # which is logged instead -- see evaluate(). With a baseline set, silence no
    # longer stops the rider, so this is the only way they can learn of it.
    degraded: bool = False
    power: float = 0.0
    # Forward deflection, 0.0-1.0, to push on the left stick while the physical
    # stick is centred. 0.0 whenever the mode is off or you are not pedalling.
    forced: float = 0.0
    # The crank sensor decided this frame: the console was paused or frozen and
    # the sensor's evidence replaced the guess. `power` is then an ESTIMATE
    # (or the sensor's cadence, for a cadence-scaled setup).
    from_sensor: bool = False
    # Sensor cadence: None when the sensor is off or not connected, or when it
    # has seen a turn but cannot yet say how fast.
    crank_rpm: float | None = None


# How far the physical stick must move, as a fraction of full deflection, before
# it takes over from forced movement. Above typical stick drift (a few percent)
# and below where a deliberate nudge lands, so steering never fights the bike.
STICK_TAKEOVER = 0.25


def left_stick(x: int, y: int, scale: float, forced: float,
               full: int = 32767) -> tuple[int, int]:
    """The left stick the virtual pad should report.

    The physical stick, scaled -- unless movement is being forced and the
    stick is centred, in which case straight forward at `forced`. Forward is
    negative Y on Linux gamepads. Either the rider steers or the bike walks; they are never summed,
    because a sum would bend every gentle strafe forward.
    """
    if forced > 0 and math.hypot(x, y) < STICK_TAKEOVER * full:
        return 0, -int(round(forced * full))
    # Uniform scalar on both components: magnitude scales, direction is
    # preserved exactly.
    return int(x * scale), int(y * scale)


class Mapper:
    def __init__(self, config: MappingConfig | None = None) -> None:
        self.config = config or MappingConfig()
        self.tracker = (CadenceTracker(stale_after=self.config.stale_after)
                        if self.config.stale_after is not None
                        else CadenceTracker())
        self._gate_open = False
        self._below_since: float | None = None
        # Raw, unsmoothed. Movement scaling reads this directly.
        self._power_raw: float = 0.0
        self._cadence_raw: float = 0.0
        self._sprinting = False
        self._at_max = False
        self._have_distance = False
        # None until the first live reading: at startup an untouched bike
        # reports zeros, and holding a scale we never measured would serve 0.0
        # for the grace window -- worse than the floor it replaces.
        self._last_live_scale: float | None = None
        # The effort that earned it, so the gauge can show what a hold is
        # standing on rather than the console's zero.
        self._last_live_effort = 0.0
        self._blackout_since: float | None = None
        self._last_reading: tuple | None = None
        self._reading_changed_at: float | None = None
        self._frozen_reported = False
        # Evidence of pedalling for forced movement: when distance last moved.
        self._distance: int | None = None
        self._distance_advanced_at: float | None = None
        # The crank sensor, fed by the bridge's sensor task. Always present so
        # callers need no None checks; it reports nothing until connected.
        self.crank = CrankTracker()
        # When the console's current run of all-zero readings began. Unlike
        # _blackout_since this is kept whether or not movement is enabled,
        # because forced movement needs it too.
        self._zeros_since: float | None = None
        # The last live (cadence, power) pair, the reference for estimating
        # power from the sensor's cadence during a pause.
        self._live_ref: tuple[float, float] | None = None
        # Set by _movement() each frame, read by evaluate().
        self._from_sensor = False
        self._effort = 0.0

    def submit(self, cadence_rpm: float, power_w: float = 0.0,
               now: float | None = None, distance: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._cadence_raw = cadence_rpm
        self._power_raw = power_w

        # Freeze detection. Distance is the discriminator and effectively the
        # only one: at fixed resistance the console derives power from cadence
        # (watts = 2 * (rpm - 25)), so those two are one signal, not two. The
        # accumulator is independent and climbs on essentially every sample
        # while the crank turns -- measured at 5-8 units per sample across
        # 55-81 rpm. Its reset-to-0 on stopping and its uint16 wrap are both
        # CHANGES, so neither can cause a false positive.
        #
        # Consequence worth knowing: a partial freeze -- distance latched while
        # cadence jitters 50/51 -- would evade this.
        reading = (cadence_rpm, power_w, distance)
        self._have_distance = distance is not None
        if reading != self._last_reading:
            self._last_reading = reading
            self._reading_changed_at = now
            self._frozen_reported = False

        # Any CHANGE to a non-zero distance counts as pedalling, not only an
        # increase: the accumulator is a uint16 and wraps. Changes TO zero do
        # not -- that is the console resetting it at the start of its pause.
        # The first non-zero sample after the pause also counts, which is right:
        # telemetry only comes back once you are pedalling again.
        if (distance is not None and distance != 0 and cadence_rpm > 0
                and self._distance is not None and distance != self._distance):
            self._distance_advanced_at = now
        self._distance = distance

        if cadence_rpm <= 0 and power_w <= 0:
            if self._zeros_since is None:
                self._zeros_since = now
        else:
            self._zeros_since = None
            if cadence_rpm > 0 and power_w > 0:
                self._live_ref = (cadence_rpm, power_w)

        self.tracker.submit(cadence_rpm, now)

    # --- The crank sensor ---------------------------------------------------

    def _sensor_live(self) -> bool:
        return self.config.crank_sensor.enabled and self.crank.connected

    def _console_paused(self, now: float) -> bool:
        """Live frames, all zeros: the console's pause (or an idle bike)."""
        return self._zeros_since is not None and not self.tracker.is_stale(now)

    def _sensor_pedalling_through_pause(self, now: float) -> bool:
        """The sensor saw a turn SINCE the pause began.

        Since, not merely recently: the sensor holds "turning" for up to a few
        seconds after the last revolution, which would otherwise carry the
        rider's stop across the freeze into the pause and walk them again for a
        moment after they had already stopped.
        """
        if not (self._sensor_live() and self._console_paused(now)):
            return False
        turned = self.crank.last_turn_at
        return (turned is not None and turned >= self._zeros_since
                and self.crank.turning(now))

    def _sensor_says_stopped_in_freeze(self, now: float) -> bool:
        """Console still reporting effort, but nothing is turning.

        Needs both witnesses: distance stalled (the console's own tell for the
        freeze) AND the sensor seeing no turns. Either alone is too weak -- a
        sensor can miss a notification, and distance needs a console that
        reports it.
        """
        if not self._sensor_live() or not self._have_distance:
            return False
        if self._zeros_since is not None or self.tracker.is_stale(now):
            return False
        if self.crank.turning(now):
            return False
        release = self.config.forced_movement.release_after
        return (self._distance_advanced_at is None
                or now - self._distance_advanced_at > release)

    def _sensor_effort(self, now: float) -> float | None:
        """Effort in the movement source's units, from the sensor's cadence.

        Power is estimated by scaling the last live reading: watts per rpm at
        the resistance you were just riding. The console's own power is close to
        linear in cadence at a fixed resistance but with an offset, so this
        over-reads somewhat when you resume much slower than you stopped. It
        only has to last the few seconds until the console returns.
        """
        rpm = self.crank.rpm(now)
        if rpm is None:
            return None
        if self.config.movement.source == "cadence":
            return rpm
        if self._live_ref is None:
            return None
        ref_cadence, ref_power = self._live_ref
        return ref_power * rpm / ref_cadence

    def is_pedalling(self, now: float | None = None) -> bool:
        """True while the distance accumulator is still climbing -- or, during
        the console's pause, while the crank sensor sees the crank turning."""
        now = time.monotonic() if now is None else now
        if self._sensor_pedalling_through_pause(now):
            return True
        if self._distance_advanced_at is None or self.tracker.is_stale(now):
            return False
        release = self.config.forced_movement.release_after
        return now - self._distance_advanced_at <= release

    def is_frozen(self, now: float | None = None) -> bool:
        """True when the console has been repeating one reading too long.

        Only meaningful while it claims movement -- an idle bike legitimately
        repeats zeros forever.
        """
        # Needs distance: without it, a steady rider holding one integer
        # cadence is indistinguishable from a console repeating itself, and
        # stopping a real rider is the worse error. Also disabled by setting
        # frozen_after to 0.
        if not self._have_distance or self.config.frozen_after <= 0:
            return False
        # A dead link satisfies every condition below -- _cadence_raw and
        # _have_distance are sticky, and _reading_changed_at keeps ageing during
        # silence. Reporting that as a frozen CONSOLE would send the next person
        # debugging the journal after a bike fault that was a radio fault.
        if self.tracker.is_stale(now):
            return False
        if self._cadence_raw <= 0 or self._reading_changed_at is None:
            return False
        now = time.monotonic() if now is None else now
        return (now - self._reading_changed_at) > self.config.frozen_after

    def _movement(self, stale: bool, now: float) -> tuple[float, bool, bool]:
        """Return (scale, sprint, at_max). Raw and unsmoothed by design."""
        self._from_sensor = False
        self._effort = self._power_raw
        movement = self.config.movement
        if not movement.enabled:
            return 1.0, False, False
        # A dead feed must not keep granting the movement the rider earned
        # while the bike was still talking. This is the fail-safe, not
        # smoothing. Note it is not protecting against a runaway rig: the scale
        # multiplies the physical stick, so an untouched controller is already
        # zero whatever this returns. See the baseline note below.
        if stale:
            self._sprinting = False
            self._at_max = False
            # Down to the baseline, not to zero.
            #
            # The invariant that matters is that a BROKEN bike never grants more
            # movement than a working one, and this preserves it exactly: a dead
            # bike gives what a live bike gives at 0 W. It is safe to relax the
            # hard stop because movement_scale multiplies the PHYSICAL stick --
            # an unattended rig does not move, because 0 x 0.5 is still 0.
            #
            # The cost is that a fault no longer announces itself by stopping
            # you; output_loop fires a haptic cue instead. With floor 0 this is
            # the old hard stop.
            return movement.floor, False, False

        # The console's pause, seen from here: live frames still arriving, but
        # carrying nothing at all. Distance is deliberately not part of the test
        # -- it reads 0 on an untouched bike too, so it adds no discrimination.
        blacked_out = self._power_raw <= 0 and self._cadence_raw <= 0
        if not blacked_out:
            self._blackout_since = None
        else:
            if self._blackout_since is None:
                self._blackout_since = now

        # The crank sensor, when there is one, replaces the guesses below.
        if self._sensor_says_stopped_in_freeze(now) or (
                blacked_out and self._sensor_live()
                and not self._sensor_pedalling_through_pause(now)):
            # Stopped, and the sensor proves it: the floor, now.
            self._from_sensor = True
            self._effort = 0.0
            self._sprinting = False
            self._at_max = False
            return movement.floor, False, False
        sensed = None
        if blacked_out and self._sensor_pedalling_through_pause(now):
            sensed = self._sensor_effort(now)
            self._from_sensor = True
            if sensed is None:
                # Turning, rate not measured yet (the first revolution after a
                # rest). Pedalling is proven, effort is not: hold what was last
                # earned, or the floor if nothing ever was.
                self._sprinting = False
                held = self._last_live_scale
                self._effort = 0.0 if held is None else self._last_live_effort
                return (movement.floor if held is None else held), False, False
            self._effort = sensed

        if blacked_out and sensed is None:
            if (movement.blackout_grace > 0
                    and self._last_live_scale is not None
                    and now - self._blackout_since < movement.blackout_grace):
                # Sprint is deliberately NOT held through the grace. Holding the
                # scale keeps the stick where it was; latching a BUTTON on no
                # evidence is a bigger promise, and releasing it is the quieter
                # way to be wrong.
                self._sprinting = False
                return self._last_live_scale, False, self._at_max

        if sensed is not None:
            value = sensed
        else:
            value = (self._power_raw if movement.source == "power"
                     else self._cadence_raw)

        span = max(1e-6, movement.max_value - movement.min_value)
        fraction = (value - movement.min_value) / span
        fraction = min(1.0, max(0.0, fraction))
        # The baseline applies at every effort, including none -- no special
        # case at zero. Effort scales the REMAINING headroom above it.
        scale = movement.floor + fraction * (1.0 - movement.floor)

        # Both flags are hysteretic: they latch on at the threshold and release
        # below it, so effort wobbling around the boundary does not chatter.
        if movement.sprint_at is not None:
            release = movement.sprint_at * movement.sprint_release_ratio
            if self._sprinting:
                self._sprinting = value >= release
            else:
                self._sprinting = value >= movement.sprint_at
        else:
            self._sprinting = False

        if self._at_max:
            self._at_max = fraction >= 0.95
        else:
            self._at_max = fraction >= 1.0

        # Recorded after the blackout test, so it only ever holds a scale
        # computed from telemetry the console actually stood behind. During the
        # ~2s freeze the console repeats its last live reading, so this captures
        # that -- which is exactly the value worth holding. An estimate from
        # the sensor is not recorded: the hold is for what the console stood
        # behind.
        if sensed is None:
            self._last_live_scale = scale
            self._last_live_effort = value
        return scale, self._sprinting, self._at_max

    def _update_gate(self, cadence: float, now: float, stale: bool) -> bool:
        gate = self.config.gate
        if not gate.enabled:
            return True

        # Grace exists for "one slow pedal stroke", not "the radio is gone".
        # A dead feed closes the gate immediately rather than buying extra
        # seconds of movement control from a bike nobody is riding.
        if stale:
            self._gate_open = False
            self._below_since = None
            return False

        if self._gate_open:
            if cadence < gate.close_rpm:
                # Start (or continue) the grace countdown before actually closing.
                if self._below_since is None:
                    self._below_since = now
                elif now - self._below_since >= gate.grace_seconds:
                    self._gate_open = False
                    self._below_since = None
            else:
                self._below_since = None
        elif cadence >= gate.open_rpm:
            self._gate_open = True
            self._below_since = None
        return self._gate_open

    def evaluate(self, now: float | None = None) -> MappingOutput:
        now = time.monotonic() if now is None else now
        cadence = self.tracker.value(now)
        out = MappingOutput(cadence=cadence)

        frozen = self.is_frozen(now)
        if frozen and not self._frozen_reported:
            self._frozen_reported = True
            print(f"  console telemetry frozen at cadence={self._cadence_raw:.0f} "
                  f"power={self._power_raw:.0f} for >"
                  f"{self.config.frozen_after:.1f}s -- releasing held buttons",
                  flush=True)
        # Frozen is deliberately NOT folded into stale, and deliberately does
        # not touch movement.
        #
        # It used to do both: a latch dropped the scale to the floor. But the
        # left stick is only ever the PHYSICAL stick times this scale, so a
        # lying console cannot move anyone on its own -- and pinning a rider who
        # is pedalling hard to the floor, for as long as the console chooses to
        # stay latched, is a worse outcome in a game than briefly over-reading
        # their effort. Nothing here is safety-critical enough to buy with that.
        #
        # What a latch genuinely breaks is the bike-driven BUTTONS below, which
        # are written to the pad whatever the rider's hands are doing. A console
        # latched above sprint_at holds sprint down for as long as it lies, with
        # the controller untouched. That is the part worth fixing, and the only
        # part.
        stale = self.tracker.is_stale(now)
        out.gate_open = self._update_gate(cadence, now, stale)
        out.movement_scale, out.sprint, out.at_max = self._movement(stale, now)
        out.from_sensor = self._from_sensor
        # The estimate while the sensor is standing in for the console, so the
        # gauge shows the effort that is actually driving movement.
        out.power = (self._effort if self._from_sensor
                     and self.config.movement.source == "power"
                     else self._power_raw)
        if self._sensor_live():
            out.crank_rpm = self.crank.rpm(now)
        # Silence still counts as degraded, which buzzes the controller. A latch
        # does not: it is logged and left alone, because a buzz mid-firefight is
        # a worse interruption than the fault it announces.
        out.degraded = stale

        # At the scale your effort earned, so working harder still walks you
        # faster. The scale is 1.0 when movement scaling is off: forcing then
        # simply means "full forward while pedalling".
        if self.config.forced_movement.enabled and self.is_pedalling(now):
            out.forced = out.movement_scale

        axis = self.config.axis
        if axis.enabled:
            span = max(1e-6, axis.max_rpm - axis.min_rpm)
            fraction = (cadence - axis.min_rpm) / span
            out.axis = min(1.0, max(0.0, fraction))

        if frozen:
            # Every bike-driven button, not just sprint: a threshold rule is the
            # same failure wearing a different name, held down for as long as
            # the console repeats the reading that opened it.
            self._sprinting = False
            self._at_max = False
            out.sprint = False
            out.at_max = False
        else:
            for rule in self.config.buttons:
                if cadence >= rule.min_rpm:
                    out.buttons.add(rule.name)

        return out
