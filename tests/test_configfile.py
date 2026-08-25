"""Tests for the config.env rewriter.

Runs with pytest, or standalone:  python3 tests/test_configfile.py

The property under test throughout is that a round trip changes the one value
asked for and NOTHING else. config.env carries the console ID, keys this
program has never heard of, and comments that are the real documentation for
why a number is what it is.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.configfile import (        # noqa: E402
    parse,
    render,
    write_values,
)
from bike_controller.dials import DialError     # noqa: E402

SAMPLE = """\
# bike-controller configuration.
XBOX_CONSOLE_ID=SECRET123
BIKE_ADDRESS=E5:AD:49:06:75:76

# --- Tuning ---
# Effort at which the left stick reaches full deflection, in watts.
MOVEMENT_MAX=75

# Raised from 0.25: 0.25 is within a rounding error of the XInput deadzone.
MOVEMENT_FLOOR=0.5
RIDE_LOG=1
"""


def test_render_changes_only_the_named_value():
    out = render(SAMPLE, {"MOVEMENT_FLOOR": "0.42"})
    before, after = SAMPLE.splitlines(), out.splitlines()
    assert len(before) == len(after)
    differing = [(a, b) for a, b in zip(before, after) if a != b]
    assert differing == [("MOVEMENT_FLOOR=0.5", "MOVEMENT_FLOOR=0.42")]


def test_render_preserves_comments_and_blank_lines():
    out = render(SAMPLE, {"MOVEMENT_MAX": "90"})
    assert "# Effort at which the left stick reaches full deflection, in watts." in out
    assert "# Raised from 0.25:" in out
    assert "\n\n# --- Tuning ---" in out


def test_render_leaves_unknown_keys_alone():
    """The console ID is the one secret here and must survive untouched."""
    out = render(SAMPLE, {"MOVEMENT_MAX": "90"})
    assert "XBOX_CONSOLE_ID=SECRET123" in out
    assert "BIKE_ADDRESS=E5:AD:49:06:75:76" in out


def test_render_appends_a_missing_key():
    out = render(SAMPLE, {"FRAME_RATE": "30"})
    assert out.splitlines()[-1] == "FRAME_RATE=30"
    assert parse(out)["FRAME_RATE"] == "30"


def test_render_rewrites_the_last_assignment_not_the_first():
    """A shell sources top to bottom, so the last assignment is the live one.

    Editing the first would leave the file looking changed and behaving
    identically -- the worst possible outcome for a page whose whole job is to
    change a number.
    """
    doubled = "MOVEMENT_FLOOR=0.1\nMOVEMENT_FLOOR=0.5\n"
    out = render(doubled, {"MOVEMENT_FLOOR": "0.7"})
    assert out == "MOVEMENT_FLOOR=0.1\nMOVEMENT_FLOOR=0.7\n"
    assert parse(out)["MOVEMENT_FLOOR"] == "0.7"


def test_render_ignores_commented_out_keys():
    text = "# MOVEMENT_FLOOR=0.9\nMOVEMENT_FLOOR=0.5\n"
    out = render(text, {"MOVEMENT_FLOOR": "0.3"})
    assert out == "# MOVEMENT_FLOOR=0.9\nMOVEMENT_FLOOR=0.3\n"


def test_render_handles_export_prefix():
    out = render("export MOVEMENT_MAX=75\n", {"MOVEMENT_MAX": "90"})
    assert parse(out)["MOVEMENT_MAX"] == "90"
    assert out.count("MOVEMENT_MAX") == 1, "an export line was duplicated"


def test_render_keeps_a_trailing_newline():
    assert render(SAMPLE, {"MOVEMENT_MAX": "90"}).endswith("\n")


def test_parse_takes_the_last_assignment():
    assert parse("A=1\nA=2\n")["A"] == "2"


# --- the filesystem half ---------------------------------------------------

def _temp_config(text=SAMPLE):
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write(text)
    handle.close()
    return handle.name


def test_write_values_round_trips():
    path = _temp_config()
    try:
        write_values(path, {"MOVEMENT_FLOOR": 0.42})
        with open(path) as handle:
            assert parse(handle.read())["MOVEMENT_FLOOR"] == "0.42"
    finally:
        os.unlink(path)


def test_write_values_leaves_no_temp_file_behind():
    path = _temp_config()
    directory = os.path.dirname(path)
    before = set(os.listdir(directory))
    try:
        write_values(path, {"MOVEMENT_MAX": 90})
        assert set(os.listdir(directory)) == before
    finally:
        os.unlink(path)


def test_write_values_refuses_keys_outside_the_dial_table():
    """The allowlist is what makes the unauthenticated page safe.

    Without it, a POST could rewrite XBOX_CONSOLE_ID or point RIDE_LOG_DIR
    anywhere on the filesystem.
    """
    path = _temp_config()
    try:
        for key in ("XBOX_CONSOLE_ID", "RIDE_LOG_DIR", "PATH"):
            try:
                write_values(path, {key: "anything"})
            except DialError:
                continue
            raise AssertionError(f"{key} was accepted")
        with open(path) as handle:
            assert "SECRET123" in handle.read()
    finally:
        os.unlink(path)


def test_write_values_creates_a_missing_file():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "config.env")
    try:
        write_values(path, {"MOVEMENT_MAX": 90})
        with open(path) as handle:
            assert parse(handle.read())["MOVEMENT_MAX"] == "90"
    finally:
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(directory)


def test_write_values_keeps_the_file_readable():
    """It runs as root; config.env is read by scripts that may not be."""
    path = _temp_config()
    try:
        os.chmod(path, 0o644)
        write_values(path, {"MOVEMENT_MAX": 90})
        assert os.stat(path).st_mode & 0o777 == 0o644
    finally:
        os.unlink(path)


def test_write_values_is_atomic():
    """The rename is the durability claim; a plain write would pass everything
    else in this file. Interrupt the write and the original must be intact.
    """
    path = _temp_config()
    import bike_controller.configfile as module

    original_render = module.render

    def exploding(text, updates):
        original_render(text, updates)
        raise RuntimeError("power cut")

    module.render = exploding
    try:
        try:
            write_values(path, {"MOVEMENT_FLOOR": 0.42})
        except RuntimeError:
            pass
        with open(path) as handle:
            text = handle.read()
        assert text == SAMPLE, "the original was damaged by a failed write"
    finally:
        module.render = original_render
        os.unlink(path)


def test_write_values_never_truncates_in_place():
    """No moment exists where config.env is a partial file.

    systemd sources it at boot, so a truncated one is a Pi that does not come
    back. Asserted by watching the inode: a rewrite in place would keep it.
    """
    path = _temp_config()
    try:
        before = os.stat(path).st_ino
        write_values(path, {"MOVEMENT_MAX": 90})
        assert os.stat(path).st_ino != before, (
            "config.env was rewritten in place rather than renamed over")
    finally:
        os.unlink(path)


def test_write_values_follows_a_symlink():
    """Severing a symlinked config.env on the first slider move is silent."""
    import tempfile as _tempfile

    directory = _tempfile.mkdtemp()
    real = os.path.join(directory, "real.env")
    link = os.path.join(directory, "config.env")
    with open(real, "w") as handle:
        handle.write(SAMPLE)
    os.symlink(real, link)
    try:
        write_values(link, {"MOVEMENT_MAX": 90})
        assert os.path.islink(link), "the symlink was replaced by a real file"
        assert parse(open(real).read())["MOVEMENT_MAX"] == "90"
    finally:
        os.unlink(link)
        os.unlink(real)
        os.rmdir(directory)


def test_render_preserves_a_trailing_comment():
    """The motivating case: the comment IS why the number is what it is."""
    text = "MOVEMENT_MAX=75  # took a ride to learn this\n"
    out = render(text, {"MOVEMENT_MAX": "90"})
    assert out == "MOVEMENT_MAX=90  # took a ride to learn this\n"


def test_render_preserves_export_and_indentation():
    assert render("export MOVEMENT_MAX=75\n", {"MOVEMENT_MAX": "90"}) \
        == "export MOVEMENT_MAX=90\n"
    assert render("    MOVEMENT_MAX=75\n", {"MOVEMENT_MAX": "90"}) \
        == "    MOVEMENT_MAX=90\n"


def test_render_preserves_crlf():
    """Rewriting CRLF as LF reports every line as changed in a diff."""
    text = "XBOX_CONSOLE_ID=SECRET\r\nMOVEMENT_MAX=75\r\n"
    out = render(text, {"MOVEMENT_MAX": "90"})
    assert out == "XBOX_CONSOLE_ID=SECRET\r\nMOVEMENT_MAX=90\r\n"


def test_write_values_preserves_ownership():
    """The bridge runs as root. A config.env that flips to root:root is one the
    desktop user can no longer edit, and install.sh's `sed -i` starts failing.

    Asserted by watching the chown CALL, not by comparing uids afterwards: the
    test file and the temp file are made by the same user, so a uid comparison
    matches whether or not the chown ever runs and could never fail.
    """
    path = _temp_config()
    calls = []
    real_chown = os.chown

    def spy(target, uid, gid):
        calls.append((uid, gid))
        return real_chown(target, uid, gid)

    os.chown = spy
    try:
        expected = os.stat(path)
        write_values(path, {"MOVEMENT_MAX": 90})
        assert calls == [(expected.st_uid, expected.st_gid)], (
            "the rewritten config.env did not inherit its owner")
        after = os.stat(path)
        assert (after.st_uid, after.st_gid) == (expected.st_uid, expected.st_gid)
    finally:
        os.chown = real_chown
        os.unlink(path)


def test_write_values_survives_a_non_utf8_file():
    """config.env is hand-edited; a degree sign in a comment is not exotic."""
    handle = tempfile.NamedTemporaryFile("wb", suffix=".env", delete=False)
    handle.write("# floor \xb0\nMOVEMENT_FLOOR=0.5\n".encode("latin-1"))
    handle.close()
    try:
        write_values(handle.name, {"MOVEMENT_FLOOR": 0.42})
        raw = open(handle.name, "rb").read()
        assert b"\xb0" in raw, "the odd byte was mangled"
        assert b"MOVEMENT_FLOOR=0.42" in raw
    finally:
        os.unlink(handle.name)


def test_write_values_formats_the_way_config_env_does():
    path = _temp_config()
    try:
        write_values(path, {"MOVEMENT_MAX": 75.0, "SPRINT_AT": None,
                            "RIDE_LOG": False})
        values = parse(open(path).read())
        assert values["MOVEMENT_MAX"] == "75"
        assert values["SPRINT_AT"] == ""
        assert values["RIDE_LOG"] == "0"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
