"""Tests for ride telemetry logging. Writes to a temp dir; no hardware."""

import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.ridelog import FIELDS, RideLogger     # noqa: E402


class Sample:
    def __init__(self, cadence=0, power=0, resistance=0):
        self.cadence_rpm, self.power_w, self.resistance = cadence, power, resistance


class FakeStatus:
    def __init__(self, move=0.0, sprint=False, gate=False):
        self.move, self.sprint, self.gate = move, sprint, gate


def rows_of(path: Path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def test_idle_bridge_leaves_no_file():
    """The bridge runs whenever the Pi is on; an unridden day must not litter."""
    with tempfile.TemporaryDirectory() as tmp:
        logger = RideLogger(tmp)
        for _ in range(50):
            logger.log(Sample(cadence=0), FakeStatus())
        logger.close()
        assert logger.path is None
        assert list(Path(tmp).iterdir()) == []


def test_pedalling_writes_rows_with_the_expected_columns():
    with tempfile.TemporaryDirectory() as tmp:
        logger = RideLogger(tmp)
        logger.log(Sample(cadence=62, power=74, resistance=3),
                   FakeStatus(move=0.98, sprint=True, gate=True))
        logger.close()

        rows = rows_of(logger.path)
        assert len(rows) == 1
        assert list(rows[0]) == FIELDS
        assert rows[0]["cadence_rpm"] == "62"
        assert rows[0]["power_w"] == "74"
        assert rows[0]["resistance"] == "3"
        assert rows[0]["movement_scale"] == "0.980"
        assert rows[0]["sprint"] == "1"
        assert rows[0]["gate_open"] == "1"


def test_coasting_within_the_grace_window_is_still_recorded():
    """Zeros during a ride are data; a stop is exactly what you want to see."""
    with tempfile.TemporaryDirectory() as tmp:
        logger = RideLogger(tmp, idle_grace=120.0)
        logger.log(Sample(cadence=60, power=70), FakeStatus(move=0.9))
        for _ in range(5):
            logger.log(Sample(cadence=0, power=0), FakeStatus())
        logger.close()
        assert len(rows_of(logger.path)) == 6


def test_rows_stop_after_the_idle_grace_expires():
    with tempfile.TemporaryDirectory() as tmp:
        logger = RideLogger(tmp, idle_grace=0.0)
        logger.log(Sample(cadence=60, power=70), FakeStatus(move=0.9))
        logger.log(Sample(cadence=0, power=0), FakeStatus())
        logger.close()
        assert len(rows_of(logger.path)) == 1


def test_logging_never_raises():
    """A logging failure must never take down the bridge mid-ride."""
    logger = RideLogger("/proc/definitely-not-writable/nope")
    logger.log(Sample(cadence=60, power=70), FakeStatus(move=0.5))
    logger.close()
    assert logger.rows == 0          # failed, but did not raise


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
