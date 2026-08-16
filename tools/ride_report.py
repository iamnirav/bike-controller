#!/usr/bin/env python3
"""Summarise ride logs, and suggest thresholds from what you actually did.

    python3 tools/ride_report.py ~/bike-rides/*.csv
    python3 tools/ride_report.py ~/bike-rides/            # every log in a dir

The point is to replace eyeballing. `--movement-max` and `--sprint-at` have been
tuned three times by feel (130 -> 100 -> 75); this reads your real power
distribution instead.
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from pathlib import Path

# What fraction of RIDING time each threshold should sit above.
#
# Full speed at the median means you spend about half a ride at full deflection
# -- enough that normal riding feels rewarded, little enough that there is
# somewhere to go. Sprint at the 90th percentile makes it a genuine push rather
# than a cruising state.
MOVEMENT_MAX_PERCENTILE = 50
SPRINT_PERCENTILE = 90


REQUIRED = ("cadence_rpm", "power_w", "movement_scale", "sprint")


def load(paths: list[Path]) -> tuple[list[dict], int]:
    """Rows, plus a count of unusable ones.

    ridelog flushes per row precisely because rides end with a power cut, so a
    torn final row is expected, not exceptional. Returning a traceback for the
    ride you just finished would be a poor reward.
    """
    rows: list[dict] = []
    skipped = 0
    for path in paths:
        with path.open() as fh:
            for row in csv.DictReader(fh):
                try:
                    if any(row.get(f) in (None, "") for f in REQUIRED):
                        raise ValueError
                    float(row["cadence_rpm"])
                    float(row["power_w"])
                    float(row["movement_scale"])
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                rows.append(row)
    return rows, skipped


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def describe(name: str, values: list[float], unit: str) -> None:
    if not values:
        print(f"  {name}: no data")
        return
    print(f"  {name:<10} median {statistics.median(values):5.0f}{unit}   "
          f"p75 {percentile(values, 75):5.0f}{unit}   "
          f"p90 {percentile(values, 90):5.0f}{unit}   "
          f"max {max(values):5.0f}{unit}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="CSV files, or a directory")
    # Defaults read from config.env's values rather than copied from the
    # systemd unit -- a second hardcoded copy of your calibration would drift
    # from the first the moment you tuned anything.
    parser.add_argument("--movement-max", type=float,
                        default=float(os.environ.get("MOVEMENT_MAX", 75)),
                        help="current setting, for comparison "
                             "(defaults to $MOVEMENT_MAX)")
    parser.add_argument("--sprint-at", type=float,
                        default=float(os.environ.get("SPRINT_AT", 100)),
                        help="current setting, for comparison "
                             "(defaults to $SPRINT_AT)")
    args = parser.parse_args()

    files: list[Path] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        files.extend(sorted(path.glob("ride-*.csv")) if path.is_dir() else [path])
    files = [f for f in files if f.exists()]
    if not files:
        print("No ride logs found.")
        return 1

    rows, skipped = load(files)
    if not rows:
        print(f"{len(files)} file(s), but no rows.")
        return 1

    # "Riding" excludes coasting: thresholds should be set from the effort you
    # actually produce, not diluted by the stops in between.
    riding = [r for r in rows if float(r["cadence_rpm"]) > 0]
    if not riding:
        print(f"{len(rows)} samples, but none while pedalling — nothing to report.")
        return 1
    powers = [float(r["power_w"]) for r in riding]
    cadences = [float(r["cadence_rpm"]) for r in riding]
    at_max = sum(1 for r in riding if float(r["movement_scale"]) >= 0.999)
    sprinting = sum(1 for r in riding if r["sprint"] == "1")
    resistances = sorted({int(float(r["resistance"])) for r in rows})

    if skipped:
        print(f"\nskipped {skipped} truncated row(s) — expected if a ride "
              f"ended with a power cut")
    print(f"\n{len(files)} ride log(s), {len(rows)} samples, "
          f"{len(riding)} while pedalling ({100 * len(riding) / len(rows):.0f}%)")
    print(f"resistance levels used: {resistances}\n")

    print("While pedalling:")
    describe("power", powers, " W")
    describe("cadence", cadences, " rpm")
    print(f"\n  at full speed: {100 * at_max / len(riding):5.1f}% of riding time")
    print(f"  sprinting:     {100 * sprinting / len(riding):5.1f}% of riding time")

    suggested_max = percentile(powers, MOVEMENT_MAX_PERCENTILE)
    suggested_sprint = percentile(powers, SPRINT_PERCENTILE)
    print("\nSuggested from this data:")
    print(f"  --movement-max {suggested_max:.0f}   "
          f"(p{MOVEMENT_MAX_PERCENTILE} of riding power; currently {args.movement_max:.0f})")
    print(f"  --sprint-at    {suggested_sprint:.0f}   "
          f"(p{SPRINT_PERCENTILE}; currently {args.sprint_at:.0f})")
    print("  Note: riding power is shaped by the CURRENT --movement-max — past "
          "full deflection\n  there is no reward for pushing harder, so a low "
          "max depresses p50 and re-suggests\n  a low max. Weigh the "
          "percentages above more than the suggestion.")

    # The percentages above are the honest check: a suggestion derived from one
    # short ride is weaker evidence than what actually happened during it.
    if at_max / len(riding) > 0.75:
        print("\n  You were at full speed most of the ride — --movement-max is low.")
    elif at_max / len(riding) < 0.10:
        print("\n  You rarely reached full speed — --movement-max is high.")
    if sprinting / len(riding) > 0.35:
        print("  Sprint was engaged for much of the ride; raise --sprint-at.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
