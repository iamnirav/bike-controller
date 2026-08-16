#!/bin/bash
# Push the current branch and update the Pi from git.
#
#   tools/deploy.sh
#
# Deliberately NOT rsync. The Pi now installs and updates exactly the way anyone
# else would -- clone from GitHub, pull, run ./install.sh -- so the documented
# path is the one that actually gets exercised. rsync meant the maintainer used a
# path no other user could, and the real one stayed untested.
#
# Optional convenience only: everything here can be done by hand with git and
# ssh. What it adds is the gate (suite + mutation testing before anything
# leaves this machine) and the self-test afterwards.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[ -f "$HERE/config.env" ] && . "$HERE/config.env"

HOST="${BIKE_PI_HOST:-${PI_HOST:-raspberrypi.local}}"
REMOTE_DIR="${BIKE_PI_PATH:-bike-controller}"
cd "$HERE"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "==> tests"
for t in tests/test_*.py; do ./.venv/bin/python "$t" | tail -1 | sed 's/^/    /'; done

# A mutant that survives means a test is not constraining what it claims to.
# Run before pushing, so a weakened suite never reaches the Pi.
echo "==> mutation testing"
python3 tools/mutate.py | tail -1 | sed 's/^/    /'

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty. Commit first -- the Pi pulls from git," >&2
    echo "       so uncommitted changes would silently not be deployed." >&2
    git status --short >&2
    exit 1
fi

echo "==> pushing $BRANCH"
git push -u origin "$BRANCH"

echo "==> updating $HOST ($REMOTE_DIR) to $BRANCH"
ssh -n "$HOST" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_DIR"
git fetch --prune origin
git checkout "$BRANCH"
# --ff-only: refuse to merge. If the Pi has diverged, that is something to look
# at by hand, not to paper over during a deploy.
git pull --ff-only origin "$BRANCH"
echo "    now at \$(git rev-parse --short HEAD) on \$(git rev-parse --abbrev-ref HEAD)"
./install.sh
REMOTE

echo "==> done"
