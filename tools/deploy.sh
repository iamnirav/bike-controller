#!/bin/bash
# Sync this repo to the Pi and restart the bridge.
#
# This machine is the source of truth; the Pi is a deployment target. Anything
# edited directly on the Pi is silently overwritten by the next run.
set -euo pipefail

HOST="${BIKE_PI_HOST:-pi-2}"
REMOTE="${BIKE_PI_PATH:-bike-controller}"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> syncing $HERE -> $HOST:$REMOTE"
rsync -az --delete \
      --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'probe-*.txt' --exclude 'scan-*.txt' \
      "$HERE/" "$HOST:$REMOTE/"

echo "==> running tests on the Pi"
# pipefail matters: without it each pipeline's status is tail's, which is always
# 0, so a red suite would print "21/22 passed" and the deploy would carry on and
# restart the service anyway. The central invariant here is a safety fail-safe
# asserted by these tests.
# Globbing matters too: naming files individually means a new test file is
# silently never run.
# `ssh host "cmd"` runs the LOGIN shell; under dash, `set -o pipefail` errors and
# execution continues with the broken semantics -- a silently absent guard,
# which is the exact failure this guard was added to prevent. Force bash.
ssh -n "$HOST" bash -s <<REMOTE_TESTS
set -o pipefail
cd $REMOTE
for t in tests/test_*.py; do
    ./.venv/bin/python "\$t" | tail -1 || exit 1
done
REMOTE_TESTS

ssh -n "$HOST" "chmod +x $REMOTE/tools/*.sh"

if [ "$RESTART" -eq 1 ]; then
    echo "==> installing unit and restarting"
    ssh -n "$HOST" "sudo cp $REMOTE/systemd/bike-bridge.service /etc/systemd/system/ && \
                    sudo systemctl daemon-reload && \
                    sudo systemctl restart bike-bridge && sleep 12 && \
                    sudo journalctl -u bike-bridge --no-pager -o cat --since '-15s' \
                      | grep -E 'Movement|Sprint|Haptics|Launch trigger|controller acquired' || true"
else
    echo "==> skipped restart"
fi
echo "==> done"
