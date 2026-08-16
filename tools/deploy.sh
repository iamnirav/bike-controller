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
ssh -n "$HOST" "cd $REMOTE && \
    ./.venv/bin/python tests/test_mapping.py  | tail -1 && \
    ./.venv/bin/python tests/test_sequence.py | tail -1 && \
    ./.venv/bin/python tests/test_cues.py     | tail -1"

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
