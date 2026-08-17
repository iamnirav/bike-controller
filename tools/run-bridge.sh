#!/bin/bash
# Build the bridge's argument list from config.env and exec it.
#
# This exists because systemd cannot omit an argument. Putting the flags in
# ExecStart means an unset BIKE_ADDRESS becomes `--address ""` -- a real,
# wrong argument rather than an absent one. Bash can just not add it.
#
# It also means tuning is a config.env edit plus a restart, with no
# daemon-reload and no editing a systemd unit to change how hard you pedal.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$REPO/config.env" ] && . "$REPO/config.env"

: "${MOVEMENT_MAX:=75}"
: "${SPRINT_AT:=100}"
: "${POLL_INTERVAL:=0.05}"
: "${RUMBLE_PASSTHROUGH:=1}"
: "${RIDE_LOG:=1}"
: "${FRAME_RATE:=60}"
: "${FROZEN_AFTER:=4}"
: "${MOVEMENT_FLOOR:=0}"
: "${DESKTOP_USER:=$(stat -c '%U' "$REPO")}"
# The real home, not /home/<user>: correct for root and for any account
# whose home is elsewhere.
: "${RIDE_LOG_DIR:=$(getent passwd "$DESKTOP_USER" | cut -d: -f6)/bike-rides}"

args=(
    --movement power
    --movement-max "$MOVEMENT_MAX"
    --sprint-at "$SPRINT_AT"
    --movement-floor "$MOVEMENT_FLOOR"
    --poll-interval "$POLL_INTERVAL"
    --frame-rate "$FRAME_RATE"
    --frozen-after "$FROZEN_AFTER"
    --launch-on-input "$REPO/tools/start-remoteplay.sh"
    --status
)
# Omitted entirely when blank, so the bridge discovers the bike by BLE name.
[ -n "${BIKE_ADDRESS:-}" ] && args+=(--address "$BIKE_ADDRESS")
[ "$RUMBLE_PASSTHROUGH" != "0" ] && args+=(--rumble-passthrough)
[ "$RIDE_LOG" != "0" ] && args+=(--ride-log "$RIDE_LOG_DIR")

exec "$REPO/.venv/bin/python" -u "$REPO/tools/bridge.py" "${args[@]}"
