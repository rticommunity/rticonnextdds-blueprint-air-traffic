#!/usr/bin/env bash
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
set -o pipefail

declare -A APP_MAP=(
    [flightplan]="app_flightplan_service.py"
    [airport]="app_airport.py"
    [tower]="app_tower.py"
    [tracon]="app_tracon.py"
    [center]="app_center.py"
    [airplane]="app_airplane.py"
    [dashboard]="app_dashboard.py"
    [weather]="app_weather_service.py"
)

# Build list of (pattern, instance_filter) pairs
searches=()

if (( $# > 0 )); then
    while (( $# > 0 )); do
        key="${1,,}"  # lowercase
        if [[ -z "${APP_MAP[$key]+_}" ]]; then
            echo "Unknown app: $1 (valid: ${!APP_MAP[*]})"
            exit 1
        fi
        pattern="${APP_MAP[$key]}"
        shift
        # Check if next arg is an instance ID (not an app name)
        instance=""
        if (( $# > 0 )); then
            next="${1,,}"
            if [[ -z "${APP_MAP[$next]+_}" ]]; then
                instance="$1"
                shift
            fi
        fi
        searches+=("$pattern|$instance")
    done
else
    for pattern in "${APP_MAP[@]}"; do
        searches+=("$pattern|")
    done
fi

# Grace period before escalating to SIGKILL. With Monitoring Library 2.0
# enabled, finalizing the participant factory takes several seconds per
# process, longer when the whole demo shuts down at once.
GRACE_SECONDS="${GRACE_SECONDS:-10}"

# Print the PIDs matching one "pattern|instance" search entry.
find_pids() {
    local pattern="${1%%|*}"
    local instance="${1#*|}"
    if [[ -n "$instance" ]]; then
        # Match processes whose command line contains both the script and the instance ID
        pgrep -f "$pattern" 2>/dev/null | while read -r pid; do
            if ps -p "$pid" -o args= 2>/dev/null | grep -q "$instance"; then
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
    ((killed++))
done

if (( killed == 0 )); then
    echo "No matching ATC demo processes found."
else
    echo "Sent SIGTERM to $killed process(es). Waiting up to ${GRACE_SECONDS}s for clean exit..."
    waited=0
    while [[ -n "$(find_all_pids)" ]] && (( waited < GRACE_SECONDS )); do
        sleep 1
        ((waited++))
    done
    # Escalate to SIGKILL for any survivors.
    survivors=0
    for pid in $(find_all_pids); do
        echo "Force-killing PID $pid (did not exit within ${GRACE_SECONDS}s of SIGTERM)"
        kill -9 "$pid" 2>/dev/null || true
        ((survivors++))
    done
    if (( survivors > 0 )); then
        echo "Force-killed $survivors stubborn process(es)."
    else
        echo "All processes exited cleanly after ${waited}s."
    fi
fi
