#!/bin/bash
# Verify the bridge on the machine it runs on. Run ON the Pi.
#
#   tools/selftest.sh smoke    # before starting the service
#   tools/selftest.sh health   # after starting it
#   tools/selftest.sh          # both, with a restart between
#
# This used to live inside tools/deploy.sh, reachable only from the maintainer's
# laptop. It is the only thing that actually proves the bridge runs, so it
# belongs with the install.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=bike-bridge
cd "$REPO"

fail() { printf '    FAILED - %s\n' "$1" >&2; exit 1; }

smoke() {
    # The banner prints BEFORE any task starts, so grepping it proves only that
    # argument parsing worked -- a bridge that dies on its first output frame
    # still emits every line. This runs the real thing and requires it to live.
    echo "==> smoke run (must survive 8s, with force feedback and a ride log)"
    sudo systemctl stop "$UNIT" 2>/dev/null || true
    sudo rm -rf /tmp/bike-selftest-rides
    set +e
    sudo timeout 8 ./.venv/bin/python -u tools/bridge.py \
        --simulate-bike --no-controller --movement power \
        --rumble-passthrough --ride-log /tmp/bike-selftest-rides \
        >/tmp/bike-selftest.log 2>&1
    rc=$?
    set -e
    # 124 is timeout(1) killing a process that was still happily running.
    [ "$rc" -eq 124 ] || { tail -15 /tmp/bike-selftest.log >&2; fail "bridge did not survive 8s (exit $rc)"; }
    grep -q 'Rumble passthrough: on' /tmp/bike-selftest.log \
        || fail "force feedback did not come up (see /tmp/bike-selftest.log)"
    ls /tmp/bike-selftest-rides/ride-*.csv >/dev/null 2>&1 \
        || fail "no ride log written"
    echo "    survived; force feedback up; ride log written"
}

health() {
    echo "==> service health"
    systemctl is-active --quiet "$UNIT" || fail "$UNIT is not active"
    # Scoped to the CURRENT invocation: a time window would surface FATALs from
    # a previous crash loop and fail a check that actually succeeded.
    local inv
    inv="$(systemctl show -p InvocationID --value "$UNIT")"
    if sudo journalctl -u "$UNIT" _SYSTEMD_INVOCATION_ID="$inv" --no-pager | grep -q FATAL; then
        sudo journalctl -u "$UNIT" _SYSTEMD_INVOCATION_ID="$inv" --no-pager -o cat \
            | grep FATAL | tail -3 >&2
        fail "FATAL in this invocation"
    fi
    sudo journalctl -u "$UNIT" _SYSTEMD_INVOCATION_ID="$inv" --no-pager -o cat \
        | grep -E '^(Movement|Sprint|Fail-safe|Haptics|Ride log|Watchdog|Launch trigger|Rumble|Web config)' \
        | sed 's/^/    /' || true
    echo "    healthy"
}

case "${1:-all}" in
    smoke)  smoke ;;
    health) health ;;
    all)    smoke
            sudo systemctl restart "$UNIT"
            sleep 12
            health ;;
    *)      echo "usage: $0 [smoke|health|all]" >&2; exit 2 ;;
esac
