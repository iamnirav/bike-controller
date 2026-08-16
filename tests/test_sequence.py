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
    detector = SequenceDetector(KONAMI, step_timeout=3.0)
    for i, token in enumerate(KONAMI[:5]):
        detector.feed(token, now=i * 0.3)
    # Long pause, then the remainder: must NOT complete.
    assert detector.index > 0
    for i, token in enumerate(KONAMI[5:]):
        detector.feed(token, now=100.0 + i * 0.3)
    assert detector.index != len(KONAMI)


def test_fires_again_on_second_entry():
    detector = SequenceDetector(KONAMI)
    assert feed_all(detector, KONAMI) is True
    assert feed_all(detector, KONAMI, t0=50.0) is True


def test_unrelated_input_is_harmless():
    detector = SequenceDetector(KONAMI)
    assert feed_all(detector, [("btn", 999)] * 20) is False


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
        except Exception as exc:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
