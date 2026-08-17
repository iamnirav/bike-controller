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
./.venv/bin/python tools/mutate.py | tail -1 | sed 's/^/    /'

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty. Commit first -- the Pi pulls from git," >&2
    echo "       so uncommitted changes would silently not be deployed." >&2
    git status --short >&2
    exit 1
fi

echo "==> pushing $BRANCH"
git push -u origin "$BRANCH"

echo "==> updating $HOST ($REMOTE_DIR) to $BRANCH"
# NOT `ssh -n`: -n redirects stdin from /dev/null, so `bash -s` reads nothing,
# runs nothing, and exits 0 -- a deploy that silently does nothing and reports
# success. The whole remote block was skipped this way once.
ssh "$HOST" bash -s <<REMOTE
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

# Verify the Pi actually moved. The remote block reporting success is not the
# same as the remote block having run -- see above.
# config.env is deliberately never synced -- it holds the Pi's own console ID
# and tuning. The cost of that is a trap: editing it here changes nothing on the
# Pi, silently. I lost an hour to exactly this, measuring a poll interval the Pi
# was not using. So say so.
if [ -f config.env ]; then
    # Compare KEYS and whether values match, never printing the values:
    # config.env holds XBOX_CONSOLE_ID, the one thing the packaging work went to
    # trouble to keep out of the repo. Deploy output ends up in screenshots.
    keyhash() { grep -vE '^\s*(#|$)' | sed 's/=.*/=/' | sort; }
    valhash() { grep -vE '^\s*(#|$)' | sort | shasum | cut -c1-8; }
    REMOTE_CFG="$(ssh "$HOST" "cat $REMOTE_DIR/config.env 2>/dev/null" </dev/null || true)"
    if [ -z "$REMOTE_CFG" ]; then
        echo "==> NOTE: $HOST has no config.env yet -- run ./install.sh there."
    elif [ "$(printf '%s\n' "$REMOTE_CFG" | valhash)" != "$(valhash < config.env)" ]; then
        echo "==> NOTE: config.env differs between here and $HOST"
        echo "    The Pi uses ITS OWN copy; nothing here was applied to it."
        DIFFKEYS="$(diff <(keyhash < config.env) <(printf '%s\n' "$REMOTE_CFG" | keyhash) \
                    | grep -oE '[A-Z_]+=' | sort -u | tr '\n' ' ')"
        [ -n "$DIFFKEYS" ] && echo "    keys only on one side: $DIFFKEYS"
        echo "    (values not shown -- config.env holds your console ID)"
        echo "    To change the Pi: ssh $HOST 'nano $REMOTE_DIR/config.env' then restart."
    fi
fi

echo "==> confirming"
LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(ssh "$HOST" "cd $REMOTE_DIR && git rev-parse HEAD" </dev/null)"
if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
    echo "ERROR: the Pi is at ${REMOTE_SHA:0:9}, expected ${LOCAL_SHA:0:9}." >&2
    echo "       The update did not take effect." >&2
    exit 1
fi
echo "    $HOST is at ${REMOTE_SHA:0:9} on $BRANCH"

echo "==> done"
