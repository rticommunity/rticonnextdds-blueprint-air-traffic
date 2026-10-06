#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# smoke_test.sh — End-to-end smoke test of the ATC demo.
#
# Starts the full demo from a temporary copy of air_traffic_scenario.json that
# runs at a higher simulation speed and is made deterministic: a fixed seed,
# no unforecast (random) weather, and one forecast storm on the KJFK-KLAX
# route.  Waits for each key interaction to appear in the application logs,
# checks the dashboard, then stops everything.
#
# Usage:
#   ./scripts/smoke_test.sh [--speed N] [--timeout SECONDS]
#
# Options:
#   --speed N        Simulation speed for the run (default: 50, the maximum)
#   --timeout S      Seconds to wait for all checks (default: 240)
#
# Requires the venv from setup.sourceme and RTI_LICENSE_FILE /
# CARTO_BASEMAP_API_KEY (normally from .env.local). Exits non-zero if any
# check fails, and keeps the demo log for inspection.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Exported so demo_start.sh runs the apps with the same interpreter (set PYTHON
# to test another environment, for example a freshly created venv).
export PYTHON="${PYTHON:-$REPO_DIR/venv/bin/python3}"
DASHBOARD_URL="http://localhost:8050"

SPEED=50
TIMEOUT=240
while (( $# > 0 )); do
    case "$1" in
        --speed)   SPEED="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -h|--help) sed -n '5,/^set /{ /^set /d; s/^# \{0,1\}//; p; }' "$0"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 2 ;;
    esac
done

# Checks, as parallel arrays: name, then an extended regex over the demo log.
CHECK_NAMES=(
    "Flight plan request/reply"
    "Handoff accepted"
    "Gate assignment request/reply"
    "Weather deviation"
)
CHECK_PATTERNS=(
    "Flight plan ACCEPTED"
    "Handoff of .* accepted by"
    "Assigned gate "
    "Weather deviation: HDG"
)

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python not found at $PYTHON. Run 'source setup.sourceme' first."
    exit 2
fi
if pgrep -f 'app_.*\.py' >/dev/null 2>&1; then
    echo "ERROR: ATC demo apps are already running. Stop them with scripts/demo_stop.sh."
    exit 2
fi

TMP_BASE="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "${TMP_BASE%/}/atc_smoke.XXXXXX")"
SCENARIO="$WORK_DIR/air_traffic_scenario.json"
LOG="$WORK_DIR/demo.log"
DEMO_PID=""

cleanup() {
    if [[ -n "$DEMO_PID" ]]; then
        "$SCRIPT_DIR/demo_stop.sh" >/dev/null 2>&1
        kill "$DEMO_PID" 2>/dev/null
        wait "$DEMO_PID" 2>/dev/null
        DEMO_PID=""
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ── Temporary deterministic scenario ──────────────────────────────────────
"$PYTHON" - "$REPO_DIR/air_traffic_scenario.json" "$SCENARIO" "$SPEED" <<'EOF' || exit 2
import datetime, json, sys
src, dst, speed = sys.argv[1], sys.argv[2], float(sys.argv[3])
with open(src) as f:
    scenario = json.load(f)
scenario["initial_sim_speed"] = speed
scenario["seed"] = 7
start = datetime.datetime.fromisoformat(scenario["sim_start_time"])
appear = (start + datetime.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
# A storm 25% of the way along KJFK-KLAX (inside ZID), where aircraft on
# that route are cruising, so the en-route center must deviate them.
scenario["weather"] = {
    "forecast_cells": [{
        "id": "WX-SMOKE-1", "sim_appear_time": appear,
        "on_airway": ["KJFK", "KLAX"], "fraction": 0.25,
        "radius_nm": 30, "severity": "SEVERE", "sim_duration_min": 240,
    }],
    "unforecast": {"mode": "off"},
}
with open(dst, "w") as f:
    json.dump(scenario, f, indent=2)
EOF

echo "Smoke test: speed ${SPEED}x, timeout ${TIMEOUT}s"
echo "Demo log: $LOG"

"$SCRIPT_DIR/demo_start.sh" all --config "$SCENARIO" --duration $(( TIMEOUT + 60 )) \
    > "$LOG" 2>&1 &
DEMO_PID=$!

# ── Wait for every log check, or the timeout ──────────────────────────────
results=()
for _ in "${CHECK_NAMES[@]}"; do results+=(""); done

start=$(date +%s)
while :; do
    elapsed=$(( $(date +%s) - start ))
    pending=0
    for i in "${!CHECK_NAMES[@]}"; do
        if [[ -z "${results[$i]}" ]]; then
            if grep -q -E "${CHECK_PATTERNS[$i]}" "$LOG" 2>/dev/null; then
                results[$i]="${elapsed}s"
            else
                pending=$(( pending + 1 ))
            fi
        fi
    done
    (( pending == 0 || elapsed >= TIMEOUT )) && break
    if ! kill -0 "$DEMO_PID" 2>/dev/null; then
        echo "ERROR: demo_start.sh exited early (see log)."
        break
    fi
    sleep 2
done

# ── Dashboard checks (while the demo is still running) ────────────────────
http_code=$(curl -s -o /dev/null -w '%{http_code}' "$DASHBOARD_URL/" 2>/dev/null)
stream=$(curl -s -m 3 "$DASHBOARD_URL/stream" 2>/dev/null | head -c 4096)

cleanup

# ── Report ────────────────────────────────────────────────────────────────
failures=0
report() {  # name, ok (0/1), detail
    if [[ "$2" == 1 ]]; then
        printf '  PASS  %-32s %s\n' "$1" "$3"
    else
        printf '  FAIL  %-32s %s\n' "$1" "$3"
        failures=$(( failures + 1 ))
    fi
}

echo ""
echo "Results:"
if [[ "$http_code" == "200" ]]; then
    report "Dashboard HTTP" 1 "200"
else
    report "Dashboard HTTP" 0 "got '${http_code:-none}'"
fi
if [[ "$stream" == *'"positions"'* && "$stream" == *'"tail_number"'* ]]; then
    report "Dashboard live positions" 1 "/stream has aircraft"
else
    report "Dashboard live positions" 0 "/stream had no aircraft positions"
fi
for i in "${!CHECK_NAMES[@]}"; do
    if [[ -n "${results[$i]}" ]]; then
        report "${CHECK_NAMES[$i]}" 1 "after ${results[$i]}"
    else
        report "${CHECK_NAMES[$i]}" 0 "not seen within ${TIMEOUT}s"
    fi
done
errors=$(grep -c -E 'Traceback|ImportError' "$LOG" 2>/dev/null)
if [[ "${errors:-0}" == 0 ]]; then
    report "No Python errors" 1 ""
else
    report "No Python errors" 0 "$errors traceback(s) in log"
fi

echo ""
if (( failures == 0 )); then
    echo "SMOKE TEST PASSED"
    rm -rf "$WORK_DIR"
    exit 0
fi
echo "SMOKE TEST FAILED ($failures check(s)). Log kept at: $LOG"
exit 1
