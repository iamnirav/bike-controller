"""Tests for the Konami-code matcher. No hardware or evdev needed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.sequence import SequenceDetector      # noqa: E402

UP, DOWN = ("hat_y", -1), ("hat_y", 1)
LEFT, RIGHT = ("hat_x", -1), ("hat_x", 1)
B, A = ("btn", 305), ("btn", 304)
KONAMI = [UP, UP, DOWN, DOWN, LEFT, RIGHT, LEFT, RIGHT, B, A]


def feed_all(detector, tokens, t0=0.0, step=0.3):
    result = False
    for i, token in enumerate(tokens):
        result = detector.feed(token, now=t0 + i * step)
    return result


def test_correct_sequence_fires():
    assert feed_all(SequenceDetector(KONAMI), KONAMI) is True


def test_partial_sequence_does_not_fire():
    assert feed_all(SequenceDetector(KONAMI), KONAMI[:-1]) is False


def test_wrong_token_resets():
    tokens = KONAMI[:4] + [RIGHT] + KONAMI[4:]
    assert feed_all(SequenceDetector(KONAMI), tokens) is False


def test_extra_leading_up_still_matches():
    """'up up up down down ...' must work.

    The third `up` is wrong at that position, but it is a valid FIRST step, so
    matching restarts at 1 rather than 0. Naive reset breaks this.
    """
    assert feed_all(SequenceDetector(KONAMI), [UP] + KONAMI) is True


def test_step_timeout_resets_progress():
    """A long pause mid-entry must abandon the attempt.

    Assert on the RETURN VALUE, not on `index`: feed() resets index to 0 the
    moment it completes, so `index != len(sequence)` is true whether or not the
    timeout works. Mutation testing caught that -- disabling the timeout
    entirely passed this test.
    """
    detector = SequenceDetector(KONAMI, step_timeout=3.0)
    for i, token in enumerate(KONAMI[:5]):
        assert detector.feed(token, now=i * 0.3) is False
    assert detector.index > 0

    fired = False
    for i, token in enumerate(KONAMI[5:]):
        fired |= detector.feed(token, now=100.0 + i * 0.3)
    assert not fired, "sequence completed despite a long pause part-way through"


def test_fires_again_on_second_entry():
    detector = SequenceDetector(KONAMI)
    assert feed_all(detector, KONAMI) is True
    assert feed_all(detector, KONAMI, t0=50.0) is True


def test_unrelated_input_is_harmless():
    detector = SequenceDetector(KONAMI)
    assert feed_all(detector, [("btn", 999)] * 20) is False


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
