"""The blackout analyser: window reconstruction and the pedalled-through filter.

These matter because the analyser is what decides whether a ride was worth
doing. Getting `pedalled_through` wrong in either direction wastes a real
person's time on a bike: too strict and a good trial is discarded, too loose
and a trial where the rider sat still gets mined for a "surviving signal" that
is really just a clock.

tools/ is not a package, so the module is loaded by path.
"""

import importlib.util
import json
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "idle_probe",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tools", "idle_probe.py"),
)
idle_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle_probe)

from tests._runner import main                                    # noqa: E402


def telemetry(cadence: int, power: int, distance: int, resistance: int = 6) -> bytes:
    """A 0x31 frame with the confirmed field offsets."""
    data = bytearray(20)
    data[0:4] = bytes([0x00, 0x12, 0x01, 0x04])
    data[5] = 0x31
    data[11] = resistance
    struct.pack_into("<H", data, 12, power)
    struct.pack_into("<H", data, 14, distance)
    data[18] = cadence
    return bytes(data)


def state_frame(counter: int, flag: int) -> bytes:
    """The `01 12 14` frame, whose byte 11 is the live/blacked-out flag."""
    data = bytearray(20)
    data[0:3] = bytes([0x01, 0x12, 0x14])
    data[7] = counter
    data[11] = flag
    return bytes(data)


def test_blackout_window_is_bounded_by_the_live_frames_either_side():
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.4, telemetry(60, 80, 103)),
        (0.8, telemetry(0, 0, 0)),
        (1.2, telemetry(0, 0, 0)),
        (1.6, telemetry(62, 84, 130)),
    ]
    windows = idle_probe.blackout_windows(frames)
    assert len(windows) == 1, windows
    assert windows[0].t0 == 0.8
    assert windows[0].t1 == 1.6
    # Measured against the last LIVE distance, not the zero reported inside.
    assert windows[0].dist_before == 103
    assert windows[0].dist_after == 130


def test_pedalling_through_is_told_from_resting_by_the_distance_delta():
    """The whole point of the filter. +3 is a coast-down; +27 is a turning wheel."""
    rested = idle_probe.Window(0.0, 5.0, dist_before=100, dist_after=103)
    pedalled = idle_probe.Window(0.0, 5.0, dist_before=100, dist_after=127)
    assert not rested.pedalled_through
    assert pedalled.pedalled_through
    assert rested.distance_delta == 3
    assert pedalled.duration == 5.0


def test_a_blackout_still_running_at_the_end_of_the_capture_is_not_a_window():
    """It has no recovery to measure and no distance-after to judge it by.

    probe-output.txt ends exactly like this, so this is the real case, not a
    hypothetical -- and counting it would report a blackout of whatever length
    the capture happened to be.
    """
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.4, telemetry(0, 0, 0)),
        (0.8, telemetry(0, 0, 0)),
    ]
    assert idle_probe.blackout_windows(frames) == []


def test_a_capture_that_never_went_live_yields_no_window():
    """Zeros before the first pedal stroke are a bike nobody is sitting on."""
    frames = [
        (0.0, telemetry(0, 0, 0)),
        (0.4, telemetry(0, 0, 0)),
        (0.8, telemetry(60, 80, 10)),
    ]
    assert idle_probe.blackout_windows(frames) == []


def test_back_to_back_blackouts_are_separate_windows():
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.4, telemetry(0, 0, 0)),
        (0.8, telemetry(60, 80, 130)),
        (1.2, telemetry(0, 0, 0)),
        (1.6, telemetry(60, 80, 160)),
    ]
    windows = idle_probe.blackout_windows(frames)
    assert len(windows) == 2, windows
    assert [w.distance_delta for w in windows] == [30, 30]


def test_non_telemetry_frames_do_not_open_or_close_a_window():
    """The state frame is 20 bytes too, and byte 5 of it is not 0x31."""
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.2, state_frame(0x70, 0x02)),
        (0.4, telemetry(0, 0, 0)),
        (0.6, state_frame(0x71, 0x03)),
        (0.8, telemetry(60, 80, 130)),
    ]
    windows = idle_probe.blackout_windows(frames)
    assert len(windows) == 1, windows
    assert windows[0].dist_before == 100


def test_moving_bytes_reports_positions_that_varied():
    group = [state_frame(0x70, 0x03), state_frame(0x71, 0x03), state_frame(0x73, 0x03)]
    moving = idle_probe.moving_bytes(group)
    # Byte 7 is the counter and moved; byte 11 held the blacked-out flag.
    assert any(m.startswith("7(") for m in moving), moving
    assert not any(m.startswith("11(") for m in moving), moving


def test_moving_bytes_on_identical_frames_is_empty():
    assert idle_probe.moving_bytes([state_frame(1, 2), state_frame(1, 2)]) == []
    assert idle_probe.moving_bytes([]) == []


def test_frame_shape_separates_telemetry_from_the_state_frame():
    """Both are 20 bytes; lumping them together makes every byte look mobile."""
    assert idle_probe.frame_shape(telemetry(1, 2, 3)) != \
        idle_probe.frame_shape(state_frame(1, 2))
    # And 0x17 filler must not share a bucket with 0x31 telemetry.
    filler = bytearray(telemetry(0, 0, 0))
    filler[5] = 0x17
    assert idle_probe.frame_shape(bytes(filler)) != \
        idle_probe.frame_shape(telemetry(0, 0, 0))


def test_read_capture_round_trips_what_the_capture_wrote():
    frames = [(0.5, telemetry(60, 80, 100)), (0.9, state_frame(3, 2))]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        for t, data in frames:
            handle.write(json.dumps({"t": t, "hex": data.hex()}) + "\n")
        path = handle.name
    try:
        assert idle_probe.read_capture(path) == frames
    finally:
        os.unlink(path)


def test_the_recovery_frame_is_not_inside_the_window():
    """t1 is the live frame that ENDED the blackout. Counting it makes the 0x31
    frame read as "moved" in every capture -- it jumps zero to real -- burying
    the surviving-signal candidates the analyser exists to surface."""
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.4, telemetry(0, 0, 0)),
        (1.0, telemetry(0, 0, 0)),
        (5.0, telemetry(60, 80, 130)),
    ]
    window = idle_probe.blackout_windows(frames)[0]
    inside = [d for t, d in frames if window.t0 <= t < window.t1]
    assert idle_probe.moving_bytes(inside) == [], inside


def test_analyse_rejects_a_capture_with_only_a_rested_blackout():
    """A non-zero exit is the signal to go and ride it again properly."""
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.4, telemetry(0, 0, 0)),
        (5.0, telemetry(60, 80, 103)),        # +3: rested
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        for t, data in frames:
            handle.write(json.dumps({"t": t, "hex": data.hex()}) + "\n")
        path = handle.name
    try:
        assert idle_probe.analyse(path) == 1
    finally:
        os.unlink(path)


def test_analyse_accepts_a_pedalled_through_blackout():
    frames = [
        (0.0, telemetry(60, 80, 100)),
        (0.4, telemetry(0, 0, 0)),
        (0.6, state_frame(0x70, 0x03)),
        (1.0, telemetry(0, 0, 0)),
        (1.2, state_frame(0x72, 0x03)),
        (5.0, telemetry(60, 80, 130)),        # +30: pedalled through
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        for t, data in frames:
            handle.write(json.dumps({"t": t, "hex": data.hex()}) + "\n")
        path = handle.name
    try:
        assert idle_probe.analyse(path) == 0
    finally:
        os.unlink(path)


def test_poke_records_are_not_mistaken_for_frames():
    """A capture holds our own writes alongside the console's notifications.

    Feeding a poke record to bytes.fromhex would crash the analyser on exactly
    the runs that have something to say -- every one where a poke fired.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        handle.write(json.dumps({"t": 0.0, "hex": telemetry(60, 80, 100).hex()}) + "\n")
        handle.write(json.dumps({"t": 0.2, "poke": "init-tail"}) + "\n")
        handle.write(json.dumps({"t": 0.4, "hex": telemetry(0, 0, 0).hex()}) + "\n")
        path = handle.name
    try:
        assert len(idle_probe.read_capture(path)) == 2
        assert idle_probe.read_pokes(path) == [0.2]
    finally:
        os.unlink(path)


def test_a_baseline_capture_has_no_pokes():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        handle.write(json.dumps({"t": 0.0, "hex": telemetry(60, 80, 100).hex()}) + "\n")
        path = handle.name
    try:
        assert idle_probe.read_pokes(path) == []
    finally:
        os.unlink(path)


if __name__ == "__main__":
    main(globals())
