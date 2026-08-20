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
    ("bike_controller/mapping.py", "frozen console still drives movement",
     "        stale = self.tracker.is_stale(now) or frozen",
     "        stale = self.tracker.is_stale(now)"),
    ("bike_controller/mapping.py", "freeze kill switch inverted",
     "        if not self._have_distance or self.config.frozen_after <= 0:",
     "        if not self._have_distance or self.config.frozen_after < 0:"),
    ("bike_controller/mapping.py", "idle bike counts as frozen",
     "        if self._cadence_raw <= 0 or self._reading_changed_at is None:",
     "        if self._reading_changed_at is None:"),
    ("bike_controller/mapping.py", "dead link blamed on the console",
     "        if self.tracker.is_stale(now):\n            return False",
     "        if False:\n            return False"),
    ("bike_controller/mapping.py", "distance dropped from the freeze tuple",
     "        reading = (cadence_rpm, power_w, distance)",
     "        reading = (cadence_rpm, power_w)"),
    ("bike_controller/sequence.py", "wobbling held stick re-fires its token",
     "            if code != axis and abs(self._raw[code]) <= STICK_THRESHOLD:",
     "            if code != axis:"),
    ("bike_controller/mapping.py", "stale ignores the baseline and hard-stops",
     "            return movement.floor, False, False",
     "            return 0.0, False, False"),
    ("bike_controller/sequence.py", "stick ignores axis dominance",
     "        if abs(x) > abs(y) and abs(x) > STICK_THRESHOLD:",
     "        if abs(x) > STICK_THRESHOLD:"),
    ("bike_controller/sequence.py", "held stick re-fires its token",
     "        if direction != previous:",
     "        if direction:"),

    # --- web config -------------------------------------------------------
    # The safety net that keeps a request from ending a ride. bridge.py's
    # on_task_done treats any escaped exception as fatal, so this is not
    # defensive clutter -- narrowing it is a stopped ride.
    ("bike_controller/webconfig.py", "a raising handler takes the bridge down",
     "        except Exception as exc:                                   # noqa: BLE001\n"
     "            # Deliberately swallowed. See the module docstring: this coroutine",
     "        except ZeroDivisionError as exc:\n"
     "            # Deliberately swallowed. See the module docstring: this coroutine"),
    # The allowlist is the whole security argument for an unauthenticated page.
    ("bike_controller/configfile.py", "config.env writer accepts any key",
     "        if dial is None:\n"
     "            raise DialError(f\"{key} is not a configurable dial\")",
     "        if dial is None:\n"
     "            continue"),
    # A shell sources top to bottom, so editing the first assignment leaves the
    # file looking changed and behaving identically.
    ("bike_controller/configfile.py", "config.env writer edits the first assignment",
     "        if match and last_line_for.get(match.group(\"key\")) == index:",
     "        if match and match.group(\"key\") in remaining:"),
    # Validate everything before applying anything: a half-applied batch leaves
    # a config the rider never asked for.
    ("bike_controller/webconfig.py", "a rejected POST still half-applies",
     "        problems = self._would_break(coerced)\n"
     "        if problems:",
     "        problems = []\n"
     "        if problems:"),
    # The three checks that stand in for a password on an unauthenticated page.
    ("bike_controller/webconfig.py", "cross-site POST accepted (Host unchecked)",
     "        if not self._is_local_host(host):",
     "        if False:"),
    ("bike_controller/webconfig.py", "cross-origin POST accepted (Origin unchecked)",
     "        if origin and self._hostname(origin) != self._hostname(host):",
     "        if False:"),
    ("bike_controller/webconfig.py", "form POST accepted (Content-Type unchecked)",
     "            if content_type.split(\";\")[0].strip().lower() != \"application/json\":",
     "            if False:"),
    # Restart-required dials with no value make the page invent one.
    ("bike_controller/webconfig.py", "restart dials report no value at all",
     "        saved = self._restart_dial_values()",
     "        saved = {}"),
    # Without `running`, the page cannot tell "you changed this, restart to
    # apply" from "this is in force".
    ("bike_controller/webconfig.py", "page cannot tell saved from running",
     "                \"running\": (None if dial.live\n"
     "                            else self.restart_values.get(dial.key)),",
     "                \"running\": None,"),
    # config.env is documentation as much as configuration.
    ("bike_controller/configfile.py", "rewrite drops trailing comments and export",
     "            out.append(match.group(\"prefix\") + remaining.pop(key)\n"
     "                       + (match.group(\"comment\") or \"\"))",
     "            out.append(f\"{key}={remaining.pop(key)}\")"),
    # A truncated config.env is a Pi that does not come back.
    ("bike_controller/configfile.py", "config.env written in place, not renamed",
     "        os.replace(handle.name, path)",
     "        open(path, \"w\", encoding=\"utf-8\").write(rendered)"),
    # Clickjacking is the one cross-site path that looks identical to a real
    # user, because the request IS made by the real page.
    ("bike_controller/webconfig.py", "page can be framed by a hostile site",
     "            f\"X-Frame-Options: DENY\\r\\n\"",
     "            f\"\""),
    # One stray byte in a hand-edited config.env used to kill the whole page.
    ("bike_controller/configfile.py", "config.env must be valid UTF-8",
     "        with open(path, \"r\", encoding=\"utf-8\", errors=\"surrogateescape\") as handle:",
     "        with open(path, \"r\", encoding=\"utf-8\") as handle:"),
    # A symlinked config.env silently severed on the first slider move.
    ("bike_controller/configfile.py", "symlinked config.env replaced, not followed",
     "    path = os.path.realpath(path)",
     "    path = path"),
    # Overlapping saves are a read-modify-write race on config.env.
    ("bike_controller/webconfig.py", "concurrent saves race on config.env",
     "            async with self._save_lock:\n"
     "                await asyncio.to_thread(write_values, self.config_path, coerced)",
     "            await asyncio.to_thread(write_values, self.config_path, coerced)"),
    # A restart dial that snaps back while saving anyway denies a change it
    # has already written to disk.
    ("bike_controller/webconfig.py", "saved restart value loses to the startup one",
     "        values = dict(self.restart_values)\n"
     "        values.update(self._persisted_values())",
     "        values = self._persisted_values()\n"
     "        values.update(self.restart_values)"),
    # Unbounded connections at Nice=-10, competing with the BLE poll loop.
    ("bike_controller/webconfig.py", "connection cap removed",
     "        if self._connections >= MAX_CONNECTIONS:",
     "        if False:"),
    ("bike_controller/webconfig.py", "connection counter leaks",
     "            self._connections -= 1",
     "            pass"),
    # bool is a subclass of int: {"FROZEN_AFTER": false} would coerce to 0.0,
    # which is in range and means "disabled".
    ("bike_controller/dials.py", "JSON booleans accepted as numbers",
     "    if isinstance(raw, bool):\n"
     "        raise DialError(f\"{dial.key} must be a number, got {raw!r}\")",
     "    if False:\n"
     "        raise DialError(f\"{dial.key} must be a number, got {raw!r}\")"),
    # config.env must stay editable by the user who owns it.
    ("bike_controller/configfile.py", "rewritten config.env loses its owner",
     "                    os.chown(handle.name, existing.st_uid, existing.st_gid)",
     "                    pass"),
    # NaN is refused by the RANGE check, because every comparison against it is
    # false and the check is written in the positive form. Inverting it to the
    # equivalent-looking negative form lets NaN straight through.
    ("bike_controller/dials.py", "range check inverted, letting NaN through",
     "    if not dial.minimum <= value <= dial.maximum:",
     "    if value < dial.minimum or value > dial.maximum:"),
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
