"""Tests for the Konami-code matcher. No hardware or evdev needed."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.sequence import (                    # noqa: E402
    EventCodes,
    SequenceDetector,
    Tokenizer,
)

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


# --- tokenizer -------------------------------------------------------------
# Linux's real numbers, so the test exercises what the bridge actually passes.
CODES = EventCodes(ev_key=1, ev_abs=3, ev_syn=0, abs_x=0, abs_y=1,
                   abs_hat_x=16, abs_hat_y=17)
FULL = 32767


class Ev:
    def __init__(self, type_, code, value):
        self.type, self.code, self.value = type_, code, value


def stick(x=0, y=0):
    """Both axes plus the SYN that ends the batch, as a real stick reports."""
    return [Ev(CODES.ev_abs, CODES.abs_x, x), Ev(CODES.ev_abs, CODES.abs_y, y),
            Ev(CODES.ev_syn, 0, 0)]


def dpad(x=0, y=0):
    return [Ev(CODES.ev_abs, CODES.abs_hat_x, x), Ev(CODES.ev_abs, CODES.abs_hat_y, y),
            Ev(CODES.ev_syn, 0, 0)]


def tokens_for(events):
    tok = Tokenizer(CODES)
    return [t for t in (tok.token(ev) for ev in events) if t is not None]


def test_cardinal_stick_pushes_produce_one_token_each():
    assert tokens_for(stick(y=-FULL)) == [UP]
    assert tokens_for(stick(x=FULL)) == [RIGHT]


def test_a_diagonal_push_does_not_emit_both_axes():
    """The bug this class exists to prevent.

    At 45 degrees each axis reads 23170 against a 19660 threshold, so a bare
    magnitude test fires BOTH tokens. The spurious one makes the detector
    backtrack, and leaning ~40 degrees off-axis made the code unenterable.
    """
    diag = int(FULL * 0.707)
    assert tokens_for(stick(x=diag, y=-diag)) == [], "diagonal emitted a token"

    # 40 degrees off vertical: still clears the threshold on both axes.
    import math
    off = stick(x=int(FULL * math.sin(math.radians(40))),
                y=-int(FULL * math.cos(math.radians(40))))
    assert tokens_for(off) == [UP], f"expected only UP, got {tokens_for(off)}"


def test_a_held_stick_fires_once():
    tok = Tokenizer(CODES)
    out = []
    for _ in range(5):
        out += [tok.token(ev) for ev in stick(y=-FULL)]
    assert [t for t in out if t] == [UP], "a held stick repeated its token"


def test_stick_must_recentre_before_firing_again():
    tok = Tokenizer(CODES)
    def push(**kw):
        return [t for t in (tok.token(ev) for ev in stick(**kw)) if t]
    assert push(y=-FULL) == [UP]
    assert push() == []
    assert push(y=-FULL) == [UP]


def test_the_whole_code_can_be_entered_with_the_stick():
    tok = Tokenizer(CODES)
    detector = SequenceDetector(KONAMI)
    events = []
    for x, y in ((0, -FULL), (0, 0), (0, -FULL), (0, 0),
                 (0, FULL), (0, 0), (0, FULL), (0, 0),
                 (-FULL, 0), (0, 0), (FULL, 0), (0, 0),
                 (-FULL, 0), (0, 0), (FULL, 0), (0, 0)):
        events += stick(x, y)
    events += [Ev(CODES.ev_key, B[1], 1), Ev(CODES.ev_key, A[1], 1)]

    fired = False
    for i, ev in enumerate(events):
        t = tok.token(ev)
        if t is not None:
            fired |= detector.feed(t, now=i * 0.2)
    assert fired, "the code could not be entered with the stick"


def test_dpad_still_works():
    tok = Tokenizer(CODES)
    detector = SequenceDetector(KONAMI)
    seq = [(0, -1), (0, -1), (0, 1), (0, 1), (-1, 0), (1, 0), (-1, 0), (1, 0)]
    fired = False
    n = 0
    for x, y in seq:
        for ev in dpad(x, y):
            t = tok.token(ev)
            if t is not None:
                n += 1
                fired |= detector.feed(t, now=n * 0.2)
    for code in (B[1], A[1]):
        n += 1
        fired |= detector.feed(("btn", code), now=n * 0.2)
    assert fired, "d-pad entry regressed"


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
