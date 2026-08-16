"""Fixed-sequence input matching (the Konami code).

Kept free of evdev so it can be unit-tested anywhere. Callers reduce their input
events to comparable tokens and feed them in; this only compares tuples.
"""

from __future__ import annotations

import time


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
