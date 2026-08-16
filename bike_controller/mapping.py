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

The console only reports at ~0.87 Hz, so raw cadence is far too steppy to drive
an axis directly. CadenceTracker smooths it and, critically, decays toward zero
when samples stop arriving -- otherwise a dropped BLE link would leave the gate
stuck open with the game happily accepting input from a stationary bike.
"""

from __future__ import annotations

import math
import time
from typing import Literal
from dataclasses import dataclass, field


@dataclass
class CadenceTracker:
    """Smooths a slow, jittery cadence signal into something usable per-frame.

    `smoothing` is the EMA weight applied per second of elapsed time, so the
    filter behaves the same regardless of how fast the caller polls it.
    """

    smoothing_per_second: float = 3.0
    # How long without a sample before the feed counts as dead. Sized in MISSED
    # FRAMES, not seconds: at the deployed 2.56 Hz telemetry rate this is ~4
    # missed frames. It was 2.5s when telemetry ran at 0.87 Hz (~2 frames); the
    # poll rate tripled and this did not, leaving the fail-safe far laxer than
    # designed. If you change --poll-interval, revisit this.
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

    With `min_value = 0` the game's own deadzone becomes the lower threshold --
    you must work hard enough to clear it before you move. That is typically
    12.5% (Unity default) to 24% (XInput recommended) of full deflection, so it
    is a real threshold, and it self-calibrates to whatever game you are in.
    """

    enabled: bool = False
    source: Literal["power", "cadence"] = "power"
    min_value: float = 0.0
    max_value: float = 130.0
    # Minimum scale once above min_value. 0.0 means pure scaling: the game's
    # deadzone is the only floor. Raise it to force movement to start abruptly.
    floor: float = 0.0
    sprint_at: float | None = None         # same units as `source`
    # Sprint releases below sprint_at * this. Without hysteresis, effort
    # fluctuating around the threshold makes the sprint button chatter on and
    # off several times a second.
    sprint_release_ratio: float = 0.92


@dataclass
class ButtonRule:
    """Fires while cadence is inside [min_rpm, max_rpm)."""

    name: str
    min_rpm: float
    max_rpm: float = 1e9


@dataclass
class MappingConfig:
    gate: GateConfig = field(default_factory=GateConfig)
    axis: AxisConfig = field(default_factory=AxisConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)
    buttons: list[ButtonRule] = field(default_factory=list)


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
    power: float = 0.0


class Mapper:
    def __init__(self, config: MappingConfig | None = None) -> None:
        self.config = config or MappingConfig()
        self.tracker = CadenceTracker()
        self._gate_open = False
        self._below_since: float | None = None
        # Raw, unsmoothed. Movement scaling reads this directly.
        self._power_raw: float = 0.0
        self._cadence_raw: float = 0.0
        self._sprinting = False
        self._at_max = False

    def submit(self, cadence_rpm: float, power_w: float = 0.0,
               now: float | None = None) -> None:
        self._cadence_raw = cadence_rpm
        self._power_raw = power_w
        self.tracker.submit(cadence_rpm, now)

    def _movement(self, stale: bool) -> tuple[float, bool, bool]:
        """Return (scale, sprint, at_max). Raw and unsmoothed by design."""
        movement = self.config.movement
        if not movement.enabled:
            return 1.0, False, False
        # A dead feed must not leave the stick deflected -- that would walk the
        # character into a wall forever. This is the fail-safe, not smoothing.
        if stale:
            self._sprinting = False
            self._at_max = False
            return 0.0, False, False

        value = self._power_raw if movement.source == "power" else self._cadence_raw

        span = max(1e-6, movement.max_value - movement.min_value)
        fraction = (value - movement.min_value) / span
        fraction = min(1.0, max(0.0, fraction))
        scale = 0.0 if fraction <= 0.0 else movement.floor + fraction * (1.0 - movement.floor)

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

        stale = self.tracker.is_stale(now)
        out.gate_open = self._update_gate(cadence, now, stale)
        out.power = self._power_raw
        out.movement_scale, out.sprint, out.at_max = self._movement(stale)

        axis = self.config.axis
        if axis.enabled:
            span = max(1e-6, axis.max_rpm - axis.min_rpm)
            fraction = (cadence - axis.min_rpm) / span
            out.axis = min(1.0, max(0.0, fraction))

        for rule in self.config.buttons:
            if rule.min_rpm <= cadence < rule.max_rpm:
                out.buttons.add(rule.name)

        return out
