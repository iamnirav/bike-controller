"""Fixed-sequence input matching (the Konami code).

Kept free of evdev so it can be unit-tested anywhere. Callers reduce their input
events to comparable tokens and feed them in; this only compares tuples.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


class SequenceDetector:
    """Matches a fixed sequence of tokens, with a per-step timeout.

    Uses KMP-style backtracking rather than resetting to zero on a mismatch.
    That matters for a real fumbled entry: with the Konami code, "up up UP down
    down ..." should still match, because the third `up` can be re-read as the
    second `up` of a fresh attempt. A naive reset (even one that restarts at 1)
    drops that, because it discards the token instead of re-testing it at the
    fallback position.
    """

    def __init__(self, sequence: list[tuple], step_timeout: float = 3.0) -> None:
        if not sequence:
            raise ValueError("sequence must not be empty")
        self.sequence = sequence
        self.step_timeout = step_timeout
        self.index = 0
        self._last = 0.0
        self._failure = self._build_failure(sequence)

    @staticmethod
    def _build_failure(sequence: list[tuple]) -> list[int]:
        """Longest proper prefix that is also a suffix, per position."""
        failure = [0] * len(sequence)
        k = 0
        for i in range(1, len(sequence)):
            while k and sequence[i] != sequence[k]:
                k = failure[k - 1]
            if sequence[i] == sequence[k]:
                k += 1
            failure[i] = k
        return failure

    def reset(self) -> None:
        self.index = 0

    def feed(self, token: tuple, now: float | None = None) -> bool:
        """Feed one token. Returns True exactly when the sequence completes."""
        now = time.monotonic() if now is None else now
        if self.index and now - self._last > self.step_timeout:
            self.index = 0                      # too slow; start over
        self._last = now

        # Fall back through the failure table until this token either matches
        # or we are back at the start -- this re-tests the token at each
        # fallback position instead of throwing it away.
        while self.index and token != self.sequence[self.index]:
            self.index = self._failure[self.index - 1]

        if token == self.sequence[self.index]:
            self.index += 1

        if self.index == len(self.sequence):
            self.index = 0
            return True
        return False


@dataclass(frozen=True)
class EventCodes:
    """The evdev codes the tokenizer needs, injected so this module stays free
    of the evdev import and therefore testable on any machine."""

    ev_key: int
    ev_abs: int
    ev_syn: int
    abs_x: int
    abs_y: int
    abs_hat_x: int
    abs_hat_y: int


# A stick is analog, so a direction has to be a THRESHOLD crossing rather than
# a value. 60% of full deflection is clear of any drift.
STICK_THRESHOLD = 0.60 * 32767


class Tokenizer:
    """Reduce input events to comparable tokens for SequenceDetector.

    Accepts the left stick as well as the d-pad: nobody entering the Konami code
    thinks about which one they are holding, and a stick push arrives on
    different axes entirely, so it used to produce no token at all.

    Stick directions are edge-triggered and require the axis to DOMINATE. A bare
    threshold is not enough -- a 45-degree push puts 23170 on each axis against a
    19660 threshold, so any lean more than ~37 degrees off a cardinal fires the
    perpendicular token too, and the detector backtracks over it. That made the
    code unenterable by stick, which is the bug this class was written to fix.

    Dominance is judged at the SYN that ends each event batch, not on the axis
    events themselves. A stick reports X and Y as separate events in one batch,
    so deciding per event lets the first one compare against a stale value for
    the other axis -- and a true 45-degree push would still emit a token.
    """

    def __init__(self, codes: EventCodes) -> None:
        self.codes = codes
        self._sticks = (codes.abs_x, codes.abs_y)
        self._emitted = {codes.abs_x: 0, codes.abs_y: 0}
        self._raw = {codes.abs_x: 0, codes.abs_y: 0}

    def token(self, event) -> tuple | None:
        c = self.codes
        if event.type == c.ev_key and event.value == 1:
            return ("btn", event.code)
        if event.type == c.ev_syn:
            return self._stick_token()
        if event.type != c.ev_abs:
            return None
        if event.code == c.abs_hat_y and event.value != 0:
            return ("hat_y", 1 if event.value > 0 else -1)
        if event.code == c.abs_hat_x and event.value != 0:
            return ("hat_x", 1 if event.value > 0 else -1)
        if event.code not in self._sticks:
            return None

        self._raw[event.code] = event.value      # decided at the next SYN
        return None

    def _stick_token(self) -> tuple | None:
        c = self.codes
        x, y = self._raw[c.abs_x], self._raw[c.abs_y]
        # One axis at most: the dominant one, and only if it clears the
        # threshold. A true diagonal has no dominant axis and emits nothing.
        if abs(x) > abs(y) and abs(x) > STICK_THRESHOLD:
            axis, value = c.abs_x, x
        elif abs(y) > abs(x) and abs(y) > STICK_THRESHOLD:
            axis, value = c.abs_y, y
        else:
            axis, value = None, 0

        direction = (1 if value > 0 else -1) if axis is not None else 0
        for code in self._sticks:
            if code != axis:
                self._emitted[code] = 0
        if axis is None:
            return None
        previous, self._emitted[axis] = self._emitted[axis], direction
        if direction != previous:
            return ("hat_y" if axis == c.abs_y else "hat_x", direction)
        return None
