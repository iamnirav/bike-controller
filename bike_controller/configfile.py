"""Read and rewrite config.env without destroying it.

config.env is a hand-edited file: it carries the console ID, keys this program
knows nothing about, and comments that are the real documentation for why a
number is what it is -- the deployed copy explains its movement floor in four
lines that took a ride to learn. So this is a line rewriter, not a dump. A
round trip through it changes the one value asked for and nothing else, byte
for byte.

Two safety properties, both deliberate:

  allowlist   Only keys in dials.DIALS can be written. XBOX_CONSOLE_ID,
              BIKE_ADDRESS, DESKTOP_USER and RIDE_LOG_DIR are therefore
              unreachable from anything built on this, which matters because
              the web page is unauthenticated and the console ID is the one
              secret in the checkout -- tools/deploy.sh goes to some trouble
              never even to print it.

  atomic      Written to a temp file in the same directory and renamed over the
              original. config.env is sourced by systemd at boot; a half-written
              one is a Pi that does not come back.

  in place    A symlinked config.env is FOLLOWED, not replaced, and the
              original owner is preserved. Both are consequences of os.replace
              creating a new inode: without them the first slider move would
              quietly sever a symlink into a dotfiles checkout, and would flip
              the file to root:root -- after which the desktop user can no
              longer edit it and install.sh's own `sed -i` fails.

No BLE, evdev or HTTP dependency, so this tests on a laptop.
"""

from __future__ import annotations

import os
import re
import tempfile

from .dials import BY_KEY, DialError, Dial, format_value

# KEY=value, tolerating leading whitespace and an `export ` prefix, because a
# hand-edited file may well have either and silently appending a duplicate key
# would mean the file says two different things and the shell believes the last.
#
# Split into three groups so a rewrite can put the line back the way it found
# it. Rebuilding the line as a bare `KEY=value` looked equivalent and was not:
# it silently ate `export `, any indentation, and -- the one that matters --
# a trailing `# comment`, which in this file is the documentation for why the
# number is what it is.
_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=)"
    r"(?P<value>.*?)"
    r"(?P<comment>\s+#.*)?$")


def _line_ending(text: str) -> str:
    """Match the file's own line ending rather than imposing LF.

    A CRLF config.env rewritten with LF reports every line as changed in a
    diff, which buries the one line that actually changed.
    """
    return "\r\n" if "\r\n" in text else "\n"


def parse(text: str) -> dict[str, str]:
    """Every KEY=value in the file, last assignment winning.

    Last, not first: that is what `.` in a shell does, and this has to agree
    with how run-bridge.sh actually reads the file.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            values[match.group("key")] = match.group("value").strip()
    return values


def render(text: str, updates: dict[str, str]) -> str:
    """The file with `updates` applied, comments and everything else preserved.

    Split out from write_values so the interesting half is testable without
    touching a filesystem.
    """
    remaining = dict(updates)
    newline = _line_ending(text)
    out: list[str] = []
    # Rewrite the LAST assignment to each key, not the first: a shell sources
    # top to bottom, so the last one is the value in force. Editing the first
    # would leave the file looking changed and behaving identically -- the worst
    # possible outcome for a page whose whole job is to change a number.
    last_line_for: dict[str, int] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match and match.group("key") in remaining:
            last_line_for[match.group("key")] = index

    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line) if not line.lstrip().startswith("#") else None
        if match and last_line_for.get(match.group("key")) == index:
            key = match.group("key")
            # prefix carries the indentation, any `export `, and the `=`;
            # comment carries a trailing `# why this number` untouched.
            out.append(match.group("prefix") + remaining.pop(key)
                       + (match.group("comment") or ""))
        else:
            out.append(line)

    # Anything the file never mentioned gets appended.
    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    rendered = newline.join(out)
    # Keep the trailing newline a text file is supposed to have. An empty file
    # stays empty rather than becoming a lone newline.
    return rendered + newline if rendered else rendered


def write_values(path: str, values: dict[str, object]) -> dict[str, str]:
    """Persist dial values to config.env. Returns what was written.

    Raises DialError for any key outside the dial table -- callers pass this
    straight through from a network request, so "unknown key" has to be a
    refusal rather than a silently created line.
    """
    updates: dict[str, str] = {}
    for key, value in values.items():
        dial: Dial | None = BY_KEY.get(key)
        if dial is None:
            raise DialError(f"{key} is not a configurable dial")
        updates[key] = format_value(dial, value)

    # Resolve before doing anything: the rename below creates a new inode, so
    # writing to the link's own name would replace the link rather than the
    # file it points at.
    path = os.path.realpath(path)

    try:
        # surrogateescape, not strict: config.env is hand-edited and a single
        # non-UTF-8 byte anywhere in it -- a degree sign in a comment -- would
        # otherwise make every save fail. The same codec on the way out puts
        # those bytes back exactly as they were.
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            original = handle.read()
    except FileNotFoundError:
        original = ""

    rendered = render(original, updates)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", errors="surrogateescape", dir=directory,
        prefix=".config.env.", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(rendered)
            handle.flush()
            # The rename below is atomic, but only orders against data that has
            # actually reached the disk. Without this, a power cut just after
            # the rename can leave the new name pointing at zero bytes -- and
            # config.env is sourced by systemd at boot.
            os.fsync(handle.fileno())
        # Carry the original's permissions AND ownership rather than the 0600
        # root:root a temp file is born with here: the bridge runs as root, and
        # a config.env that flips to root-owned is one the desktop user can no
        # longer edit -- and install.sh's `sed -i` on it starts failing.
        try:
            existing = os.stat(path)
            os.chmod(handle.name, existing.st_mode & 0o7777)
            if hasattr(os, "chown"):
                try:
                    os.chown(handle.name, existing.st_uid, existing.st_gid)
                except PermissionError:
                    # Not root, and not our file. Losing ownership is bad; not
                    # saving at all is worse, and the mode is already right.
                    pass
        except FileNotFoundError:
            os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)
        # The rename itself is only durable once the DIRECTORY entry is on
        # disk. Without this the data survives a power cut and the name may
        # not, which is the same lost edit by a different route.
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass                # best effort; not worth failing a save over
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return updates
