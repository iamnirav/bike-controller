"""The tunable dials, described once.

Every knob the rider can change at runtime is one `Dial` entry below. That entry
is simultaneously its env-var name in config.env, its bounds, its units, its
label on the web page, and -- for the live ones -- where it lives inside a
MappingConfig. Adding a knob is adding a row here; nothing else needs to know.

That single-source property is the whole point. Before this module the bounds
for MOVEMENT_FLOOR were written out by hand in bridge.py's argument validation,
and any second consumer (a web form, say) would have had to restate them and
then quietly drift. build_settings() now validates through this table too, so
the CLI and the page cannot disagree about what a legal value is.

Deliberately free of any BLE, evdev or HTTP dependency, like mapping.py: this is
pure logic over a table, so it unit-tests on a laptop with no hardware.

LIVE VS RESTART-REQUIRED
------------------------
A dial with a `path` is live: Mapper re-reads its config dataclasses on every
evaluate(), so assigning through the path takes effect on the next output frame.
A dial with `path is None` only reaches the running program via a restart, and
is persisted to config.env and left there. The split is not cosmetic -- see
POLL_INTERVAL's note below for why one of them stays deliberately on the slow
side.
"""

from __future__ import annotations

from dataclasses import dataclass


class DialError(ValueError):
    """A value that no dial will accept.

    A distinct type because the web layer must answer it with 400 rather than
    500, and because argparse's own errors exit the process -- which is exactly
    what a request handler must never do.
    """


@dataclass(frozen=True)
class Dial:
    key: str                     # env var name, e.g. "MOVEMENT_FLOOR"
    label: str                   # shown on the page
    kind: str                    # "float" | "int" | "bool"
    minimum: float
    maximum: float
    step: float
    # Dotted path into MappingConfig, or None when the value can only be
    # applied by restarting. This is the live/restart discriminator; there is
    # no separate flag to keep in step with it.
    path: str | None = None
    unit: str = ""
    # None means "off" rather than a number -- SPRINT_AT is the only one today.
    nullable: bool = False
    # argparse dest this dial also validates, so bridge.py's CLI and this table
    # cannot disagree about the legal range. None for dials whose CLI form is
    # not a plain number: RIDE_LOG is a directory on the command line and a 0/1
    # in config.env, and RUMBLE_PASSTHROUGH is a store_true.
    arg: str | None = None
    # A value that is legal despite being outside [minimum, maximum], because it
    # means "disabled". FROZEN_AFTER accepts 0 or >= 2.5 and nothing between:
    # the console legitimately holds its last reading for ~2s at the end of
    # every pedalling stretch, so a shorter window zeroes movement every time
    # you stop.
    disabled_value: float | None = None
    help: str = ""

    @property
    def live(self) -> bool:
        return self.path is not None


DIALS: tuple[Dial, ...] = (
    # --- Live: applied on the next output frame ---------------------------
    Dial(
        key="MOVEMENT_MAX", arg="movement_max", label="Full deflection at", kind="float",
        # Wide on purpose. These bounds are the VALIDATION limit as well as the
        # slider's range, and argparse had no check here at all before -- so a
        # narrow "sensible" range would turn a stronger rider's existing
        # --movement-max 350 into a startup failure. Bound only what protects
        # something.
        minimum=20.0, maximum=500.0, step=5.0, unit="W",
        path="movement.max_value",
        help="Effort at which the left stick reaches full deflection. Raise "
             "this if the top end feels flat.",
    ),
    Dial(
        key="MOVEMENT_MIN", arg="movement_min", label="Movement starts at", kind="float",
        minimum=0.0, maximum=400.0, step=5.0, unit="W",
        path="movement.min_value",
        help="Effort at which movement starts. 0 lets the game's own deadzone "
             "be the threshold.",
    ),
    Dial(
        key="MOVEMENT_FLOOR", arg="movement_floor", label="Movement floor", kind="float",
        # Capped below 1.0, not at it: a floor of exactly 1.0 is full
        # deflection at zero effort, i.e. the bike disconnected from the
        # controls entirely, which is never what anyone means to set.
        minimum=0.0, maximum=0.99, step=0.01,
        path="movement.floor",
        help="Baseline multiplier you always have, at any effort including "
             "none. Tune against a game, not the arithmetic -- it has to clear "
             "the game's deadzone. 0 is strict pedal-or-nothing.",
    ),
    Dial(
        key="SPRINT_AT", arg="sprint_at", label="Sprint at", kind="float",
        minimum=20.0, maximum=600.0, step=5.0, unit="W",
        path="movement.sprint_at", nullable=True,
        help="Effort at/above which the sprint button is held. Releases at 92% "
             "of this, so hovering at the line does not chatter.",
    ),
    Dial(
        key="FROZEN_AFTER", arg="frozen_after", label="Freeze guard", kind="float",
        # Up to 120, not 30: the freeze actually observed on this hardware
        # lasted 30 seconds, so 30 is the one value you would reach for to sit
        # clear of false positives -- capping exactly there is the wrong place
        # to put a limit. argparse had no upper bound at all before.
        minimum=2.5, maximum=120.0, step=0.5, unit="s",
        path="frozen_after", disabled_value=0.0,
        help="Seconds of bit-identical telemetry before the console counts as "
             "frozen. 0 disables. Below 2.5 it would fire every time you stop.",
    ),

    # --- Restart required --------------------------------------------------
    Dial(
        key="POLL_INTERVAL", arg="poll_interval", label="BLE poll interval", kind="float",
        # Down to 0.001 to match the old `0 < v <= 0.5` exactly. Below 0.01
        # measured no better, but that is a reason not to bother, not a reason
        # to refuse to start.
        # step 0.001, not 0.01. A range input snaps to min + step*n, so with
        # step 0.01 from a 0.001 minimum the reachable values were 0.001,
        # 0.011, 0.021... and NONE of the documented ones -- 0.02 (deployed),
        # 0.05, 0.2 -- were on the grid. The slider sat at 0.021 while its own
        # label read 0.02.
        minimum=0.001, maximum=0.5, step=0.001, unit="s",
        # Capped hard: the staleness window is derived from this, so an
        # unbounded value buys seconds of full-deflection movement from a dead
        # bike. The model behind stale_after_for() is only calibrated over
        # 0.02-0.2 anyway.
        help="Seconds between BLE poll writes. Restart required: the fail-safe "
             "window is derived from this, and recomputing it live would mean "
             "changing the guard that stops a dead bike granting movement.",
    ),
    Dial(
        key="FRAME_RATE", arg="frame_rate", label="Output frame rate", kind="int",
        minimum=5, maximum=1000, step=5, unit="Hz",
        # The bounds are not arbitrary. 0 divides by zero computing the frame
        # period; negative spins the output loop at 100% CPU, and it runs at
        # Nice=-10; below the telemetry rate, ride-log rows are silently
        # dropped because each row needs a frame to carry it.
        help="Virtual pad output rate. Lower frees the event loop for BLE "
             "polling, at the cost of controller latency. Must stay above the "
             "telemetry rate or ride-log rows are dropped.",
    ),
    Dial(
        key="RUMBLE_PASSTHROUGH", label="Rumble passthrough", kind="bool",
        minimum=0, maximum=1, step=1,
        help="Forward the game's rumble to your controller. Restart required: "
             "force feedback is advertised when the virtual pad is created.",
    ),
    Dial(
        key="RIDE_LOG", label="Ride logging", kind="bool",
        minimum=0, maximum=1, step=1,
        help="Append ride telemetry to a CSV.",
    ),
)

BY_KEY: dict[str, Dial] = {d.key: d for d in DIALS}


def coerce(dial: Dial, raw: object) -> float | int | bool | None:
    """Parse and range-check one value. Raises DialError, never exits.

    Accepts the string forms too, because config.env and an HTML form both hand
    us strings and neither should have to know the dial's type.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        if dial.nullable:
            return None
        raise DialError(f"{dial.key} has no 'off' setting; give it a number")

    if dial.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise DialError(f"{dial.key} must be 0 or 1, got {raw!r}")

    # bool is a subclass of int, so it would silently pass as a number below and
    # arrive as True/False in a float field. Reject it explicitly.
    if isinstance(raw, bool):
        raise DialError(f"{dial.key} must be a number, got {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise DialError(f"{dial.key} must be a number, got {raw!r}") from None
    if dial.kind == "int" and value != int(value):
        # Refused rather than rounded. --frame-rate 62.5 used to run at 62.5 Hz;
        # silently turning it into 62 is a value change the caller never sees.
        raise DialError(f"{dial.key} must be a whole number, got {value:g}")

    if dial.disabled_value is not None and value == dial.disabled_value:
        return _as_kind(dial, value)
    # Written as `not (min <= v <= max)` rather than `v < min or v > max` so
    # that NaN is refused too: every comparison against NaN is false, so the
    # positive form fails and this rejects it. NaN reaching movement.floor
    # would be a silently dead stick. test_nan_and_infinity_are_refused pins
    # this; do not "simplify" it into the negative form.
    if not dial.minimum <= value <= dial.maximum:
        extra = (f" (or {dial.disabled_value:g} to disable)"
                 if dial.disabled_value is not None else "")
        # The help text is appended, not omitted: the messages this replaced
        # were the documentation for WHY each bound exists -- that a freeze
        # guard under 2.5s fires every time you stop pedalling, that a negative
        # frame rate spins the loop at 100% CPU at Nice=-10. A bare range tells
        # you what is refused and nothing about what to do instead.
        raise DialError(
            f"{dial.key} must be between {dial.minimum:g} and "
            f"{dial.maximum:g}{extra}, got {value:g}"
            + (f". {dial.help}" if dial.help else ""))
    return _as_kind(dial, value)


def _as_kind(dial: Dial, value: float) -> float | int:
    return int(round(value)) if dial.kind == "int" else float(value)


def read(config, dial: Dial):
    """Current value of a live dial, from the running config."""
    if dial.path is None:
        raise DialError(f"{dial.key} is not a live dial")
    target = config
    for part in dial.path.split("."):
        target = getattr(target, part)
    return target


def apply(config, dial: Dial, value) -> None:
    """Assign a live dial into the running config.

    Takes effect on the next Mapper.evaluate(), because Mapper re-reads these
    dataclasses every frame rather than caching them.
    """
    if dial.path is None:
        raise DialError(f"{dial.key} cannot be applied without a restart")
    parent_path, _, leaf = dial.path.rpartition(".")
    target = config
    for part in parent_path.split(".") if parent_path else []:
        target = getattr(target, part)
    setattr(target, leaf, value)


def check_consistency(config) -> list[str]:
    """Rules spanning two fields, which no single-field range check can catch.

    Shared by build_settings() and the web POST path on purpose: an inverted
    pair is just as broken when it arrives from a slider as from argv, and
    having one of the two callers enforce it was how they would drift apart.
    """
    problems: list[str] = []
    movement = config.movement
    if movement.max_value <= movement.min_value:
        problems.append(
            f"MOVEMENT_MAX / --movement-max ({movement.max_value:g}) must "
            f"exceed MOVEMENT_MIN / --movement-min ({movement.min_value:g})")
    gate = config.gate
    if gate.open_rpm < gate.close_rpm:
        # Named as flags: the gate has no dial, so this is only ever reached
        # from the command line, and an error naming no flag is a worse error.
        problems.append(
            f"--gate-open ({gate.open_rpm:g}) must be >= --gate-close "
            f"({gate.close_rpm:g}); inverted thresholds make the gate chatter")
    axis = config.axis
    if axis.max_rpm <= axis.min_rpm:
        problems.append(
            f"axis max ({axis.max_rpm:g}) must exceed axis min "
            f"({axis.min_rpm:g})")
    return problems


def format_value(dial: Dial, value) -> str:
    """Render a value the way config.env spells it."""
    if value is None:
        return ""
    if dial.kind == "bool":
        return "1" if value else "0"
    if dial.kind == "int":
        return str(int(value))
    # %g so 0.5 stays "0.5" rather than "0.5000000000000001" after a slider
    # round-trip, and 75.0 stays "75" the way a hand-edited config.env has it.
    return f"{float(value):g}"
