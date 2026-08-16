#!/usr/bin/env python3
"""Mutation testing: break the code deliberately, check the tests notice.

    python3 tools/mutate.py            # run every mutant
    python3 tools/mutate.py --list     # just show them

A passing suite tells you the tests ran. It does not tell you they constrain
anything. This applies a known bug to a copy of the tree and runs the suite: if
the tests still pass, that behaviour is unprotected and a future change can
silently break it.

This is not theoretical here. A review found that deleting the ENTIRE cadence
filter passed all 25 tests, because every test asserted converged steady-state
values -- by which point smoothed and raw agree. The stale-decay ramp was
likewise untested by a test whose name promised to cover it.

Every mutant below should be KILLED (tests fail). A survivor is a finding.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, description, old, new). `old` must appear exactly once.
MUTANTS = [
    ("bike_controller/mapping.py", "stale feed does not close the gate",
     "        if stale:\n            self._gate_open = False",
     "        if False:\n            self._gate_open = False"),
    ("bike_controller/mapping.py", "stale feed does not zero movement scale",
     "        if stale:\n            self._sprinting = False",
     "        if False:\n            self._sprinting = False"),
    ("bike_controller/mapping.py", "cadence smoothing removed entirely",
     "        self._value += (self._last_sample - self._value) * min(1.0, alpha)",
     "        self._value = self._last_sample"),
    ("bike_controller/mapping.py", "stale decay drops instantly instead of ramping",
     "            self._value = self._stale_from * fade",
     "            self._value = 0.0"),
    ("bike_controller/mapping.py", "staleness window loses its floor and cap",
     "    return min(3.0, max(1.5, frames_of_margin * period))",
     "    return frames_of_margin * period"),
    ("bike_controller/mapping.py", "gate reopens at the close threshold (no hysteresis)",
     "        elif cadence >= gate.open_rpm:",
     "        elif cadence >= gate.close_rpm:"),
    ("bike_controller/mapping.py", "gate grace period ignored",
     "                elif now - self._below_since >= gate.grace_seconds:",
     "                elif True:"),
    ("bike_controller/mapping.py", "sprint loses its release hysteresis",
     "                self._sprinting = value >= release",
     "                self._sprinting = value >= movement.sprint_at"),
    ("bike_controller/mapping.py", "movement fraction not clamped to 0..1",
     "        fraction = min(1.0, max(0.0, fraction))",
     "        fraction = fraction"),
    ("bike_controller/sequence.py", "sequence step timeout never resets progress",
     "        if self.index and now - self._last > self.step_timeout:",
     "        if False:"),
    ("bike_controller/sequence.py", "naive reset instead of KMP backtracking",
     "        while self.index and token != self.sequence[self.index]:\n"
     "            self.index = self._failure[self.index - 1]",
     "        if self.index and token != self.sequence[self.index]:\n"
     "            self.index = 0"),
    ("bike_controller/ridelog.py", "ride log records idle time too",
     "            if now - self._last_active > self.idle_grace:",
     "            if False and now - self._last_active > self.idle_grace:"),
    ("bike_controller/ridelog.py", "ride log never rotates between rides",
     "                if self._file is not None:\n"
     "                    self._file.close()\n"
     "                    self._file = None\n"
     "                    self._writer = None\n"
     "                return",
     "                return"),
    ("bike_controller/watchdog.py", "watchdog claims health with no working socket",
     "            else:\n                self._socket = sock",
     "            else:\n                self._socket = sock\n"
     "            self._socket = self._socket or socket.socket("
     "socket.AF_UNIX, socket.SOCK_DGRAM)"),
    # NOTE: uinput_ff.py's upload/erase handshake is deliberately NOT mutated
    # here. Exercising it needs a real /dev/uinput and a kernel, which this
    # harness does not have -- a mutant that can never be killed is noise, not
    # signal. deploy.sh covers that path by running the bridge with
    # --rumble-passthrough on the Pi and requiring the pad to advertise EV_FF.
]


def run_suite(tree: Path) -> bool:
    """True if the suite passes in `tree`."""
    for test in sorted((tree / "tests").glob("test_*.py")):
        result = subprocess.run([sys.executable, str(test)],
                                cwd=tree, capture_output=True, text=True)
        if result.returncode != 0:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show mutants and exit")
    args = parser.parse_args()

    if args.list:
        for path, description, _, _ in MUTANTS:
            print(f"  {path:<32} {description}")
        return 0

    print(f"Applying {len(MUTANTS)} mutants...\n")
    survivors, errors = [], []

    for path, description, old, new in MUTANTS:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(
                ".venv", "__pycache__", ".git", "*.csv"))

            target = tree / path
            source = target.read_text()
            if source.count(old) != 1:
                # The mutant no longer matches: the code moved on and this
                # mutant is now testing nothing. Louder than a survivor.
                errors.append((description, f"pattern matched {source.count(old)}x"))
                print(f"  STALE   {description}")
                continue
            target.write_text(source.replace(old, new, 1))

            if run_suite(tree):
                survivors.append(description)
                print(f"  SURVIVED  {description}")
            else:
                print(f"  killed    {description}")

    print()
    if errors:
        print(f"{len(errors)} mutant(s) no longer match the code — update tools/mutate.py:")
        for description, why in errors:
            print(f"  - {description} ({why})")
    if survivors:
        print(f"{len(survivors)} mutant(s) SURVIVED — these behaviours are untested:")
        for description in survivors:
            print(f"  - {description}")
        return 1
    if not errors:
        print(f"All {len(MUTANTS)} mutants killed.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
