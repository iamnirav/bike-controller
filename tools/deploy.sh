#!/bin/bash
# Sync this repo to the Pi and restart the bridge.
#
# This machine is the source of truth; the Pi is a deployment target. Anything
# edited directly on the Pi is silently overwritten by the next run.
set -euo pipefail

HERE_EARLY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[ -f "$HERE_EARLY/config.env" ] && . "$HERE_EARLY/config.env"

HOST="${BIKE_PI_HOST:-${PI_HOST:-raspberrypi.local}}"
REMOTE="${BIKE_PI_PATH:-bike-controller}"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Locally first: a mutant that survives means a test is not constraining what it
# claims to. Run before syncing, so a weakened suite never reaches the Pi.
echo "==> mutation testing (local)"
python3 "$HERE/tools/mutate.py" | tail -3

echo "==> syncing $HERE -> $HOST:$REMOTE"
# config.env is EXCLUDED deliberately: it holds the Pi's own console ID and
# tuning, and --delete would otherwise remove it on every deploy. The generated
# unit and udev rule are excluded for the same reason -- they belong to the Pi,
# not to this checkout.
rsync -az --delete \
      --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'probe-*.txt' --exclude 'scan-*.txt' \
      --exclude 'config.env' --exclude 'rides' --exclude 'ride-*.csv' \
      --exclude 'systemd/bike-bridge.service' \
      --exclude 'udev/99-bike-controller.rules' \
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
    # Smoke run BEFORE restarting. The banner prints before any task starts, so
    # grepping it proves only that argument parsing worked -- a bridge that dies
    # on its first output frame emits every line the grep looks for. This runs
    # the real thing on the real machine and requires it to survive.
    # Exercises BOTH risky paths: the ctypes force-feedback device (the most
    # likely thing to break on a kernel upgrade, and whose failure blocks the
    # browser rather than merely going quiet) and ride logging.
    echo "==> smoke run (must survive 8s, with FF and ride log)"
    ssh -n "$HOST" "sudo systemctl stop bike-bridge 2>/dev/null; \
        sudo rm -rf /tmp/smoke-rides; \
        cd $REMOTE && sudo timeout 8 ./.venv/bin/python -u tools/bridge.py \
            --simulate-bike --no-controller --movement power \
            --rumble-passthrough --ride-log /tmp/smoke-rides >/tmp/smoke.log 2>&1; \
        rc=\$?; \
        if [ \$rc -ne 124 ]; then \
            echo '    FAILED - bridge did not survive 8s:'; \
            tail -15 /tmp/smoke.log; exit 1; \
        fi; \
        grep -q 'Rumble passthrough: on' /tmp/smoke.log \
            || { echo '    FAILED - force feedback did not come up:'; \
                 grep -i rumble /tmp/smoke.log; exit 1; }; \
        ls /tmp/smoke-rides/ride-*.csv >/dev/null 2>&1 \
            || { echo '    FAILED - no ride log written'; exit 1; }; \
        echo '    survived; force feedback up; ride log written'"

    echo "==> installing unit and restarting"
    # Regenerate the unit from the template on the Pi, so a template change
    # reaches the running service without re-running install.sh.
    ssh -n "$HOST" "cd $REMOTE && \
                    sed \"s|@REPO@|\$PWD|g\" systemd/bike-bridge.service.template \
                      > systemd/bike-bridge.service && \
                    sudo cp systemd/bike-bridge.service /etc/systemd/system/ && \
                    sudo systemctl daemon-reload && \
                    sudo systemctl restart bike-bridge && sleep 12 && \
                    sudo journalctl -u bike-bridge --no-pager -o cat --since '-15s' \
                      | grep -E 'Movement|Sprint|Haptics|Launch trigger|controller acquired' || true"

    # Scoped to the CURRENT invocation: a time window would surface FATALs from
    # a previous crash loop and fail a deploy that actually fixed them.
    echo "==> post-restart health"
    ssh -n "$HOST" "sudo systemctl is-active --quiet bike-bridge \
            || { echo '    unit is not active'; exit 1; }; \
        INV=\$(systemctl show -p InvocationID --value bike-bridge); \
        if sudo journalctl -u bike-bridge _SYSTEMD_INVOCATION_ID=\$INV --no-pager \
             | grep -q FATAL; then \
            echo '    FATAL in this invocation:'; \
            sudo journalctl -u bike-bridge _SYSTEMD_INVOCATION_ID=\$INV --no-pager -o cat \
              | grep FATAL | tail -3; \
            exit 1; \
        fi; \
        echo '    healthy'"
else
    echo "==> skipped restart"
fi
echo "==> done"
