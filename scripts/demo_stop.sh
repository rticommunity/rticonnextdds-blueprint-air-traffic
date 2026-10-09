#!/bin/bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# demo_stop.sh — Stop running ATC demo applications.
#
# Usage:
#   ./demo_stop.sh                  # stop all apps
#   ./demo_stop.sh dashboard        # stop only the dashboard
#   ./demo_stop.sh airplane         # stop only airplanes
#   ./demo_stop.sh center           # stop all centers
#   ./demo_stop.sh center ZNY       # stop only center ZNY
#   ./demo_stop.sh tower KJFK       # stop only the KJFK tower
#   ./demo_stop.sh center tower     # stop all centers and towers
#
# Written for bash 3.2 (the macOS /bin/bash): no associative arrays or
# ${var,,} case conversion.
#
set -o pipefail

APP_NAMES="flightplan airport tower tracon center airplane dashboard weather"

# Print the script for an app name; fail for an unknown name.
app_script() {
    case "$1" in
        flightplan) echo "app_flightplan_service.py" ;;
        airport)    echo "app_airport.py" ;;
        tower)      echo "app_tower.py" ;;
        tracon)     echo "app_tracon.py" ;;
        center)     echo "app_center.py" ;;
        airplane)   echo "app_airplane.py" ;;
        dashboard)  echo "app_dashboard.py" ;;
        weather)    echo "app_weather_service.py" ;;
        *)          return 1 ;;
    esac
}

lowercase() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Build list of "script|instance" search entries
searches=()

if (( $# > 0 )); then
    while (( $# > 0 )); do
        if ! script="$(app_script "$(lowercase "$1")")"; then
            echo "Unknown app: $1 (valid: $APP_NAMES)"
            exit 1
        fi
        shift
        # Check if next arg is an instance ID (not an app name)
        instance=""
        if (( $# > 0 )) && ! app_script "$(lowercase "$1")" >/dev/null; then
            instance="$1"
            shift
        fi
        searches+=("$script|$instance")
    done
else
    for name in $APP_NAMES; do
        searches+=("$(app_script "$name")|")
    done
fi

# Grace period before escalating to SIGKILL. With Monitoring Library 2.0
# enabled, finalizing the participant factory takes several seconds per
# process, longer when the whole demo shuts down at once.
GRACE_SECONDS="${GRACE_SECONDS:-10}"

# Print the PIDs matching one "script|instance" search entry. Requiring the
# Python interpreter before the script keeps editors and pagers that have the
# file open (e.g. "vim python/app_center.py") from matching.
find_pids() {
    local pattern="[Pp]ython[0-9.]* .*${1%%|*}"
    local instance="${1#*|}"
    if [[ -n "$instance" ]]; then
        # Match processes whose command line also contains the instance ID
        pgrep -f "$pattern" 2>/dev/null | while read -r pid; do
            if ps -p "$pid" -o args= 2>/dev/null | grep -q -- "$instance"; then
                echo "$pid"
            fi
        done
    else
        pgrep -f "$pattern" 2>/dev/null || true
    fi
}

# Print the PIDs matching any search entry.
find_all_pids() {
    for entry in "${searches[@]}"; do
        find_pids "$entry"
    done
}

killed=0

for pid in $(find_all_pids); do
    cmdline=$(ps -p "$pid" -o args= 2>/dev/null || true)
    echo "Stopping PID $pid: $cmdline"
    kill "$pid" 2>/dev/null || true
    killed=$((killed + 1))
done

if (( killed == 0 )); then
    echo "No matching ATC demo processes found."
else
    echo "Sent SIGTERM to $killed process(es). Waiting up to ${GRACE_SECONDS}s for clean exit..."
    waited=0
    while [[ -n "$(find_all_pids)" ]] && (( waited < GRACE_SECONDS )); do
        sleep 1
        waited=$((waited + 1))
    done
    # Escalate to SIGKILL for any survivors.
    survivors=0
    for pid in $(find_all_pids); do
        echo "Force-killing PID $pid (did not exit within ${GRACE_SECONDS}s of SIGTERM)"
        kill -9 "$pid" 2>/dev/null || true
        survivors=$((survivors + 1))
    done
    if (( survivors > 0 )); then
        echo "Force-killed $survivors stubborn process(es)."
    else
        echo "All processes exited cleanly after ${waited}s."
    fi
fi
