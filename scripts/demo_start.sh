#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# demo_start.sh — Launch ATC demo applications individually or all at once.
#
# Usage:
#   ./demo_start.sh <command> [options]
#
# Commands:
#   all            Start the full scenario (all applications)
#   flightplan     Start the Flight Plan Filing Service
#   airport        Start an Airport infrastructure app
#   tower          Start a Control Tower app
#   tracon         Start a TRACON app
#   center         Start an En-Route Center app
#   airplane       Start an Aircraft simulator
#   dashboard      Start the Dashboard monitor
#   weather        Start the Weather Service (ConvectiveCell)
#   help           Show this help message
#
# Global options:
#   --duration N            Run duration in wall seconds (default: the
#                           scenario's duration_seconds)
#   --wall-start-time T     Real UTC time at which the scenario's
#                           sim_start_time happened (ISO 8601). Default: now.
#                           Every app gets the same value; "all" prints it so
#                           a single app started later can join the same clock.
#
# Examples:
#   ./demo_start.sh all
#   ./demo_start.sh all --duration 120
#   ./demo_start.sh all --config ../air_traffic_scenario.json
#   ./demo_start.sh flightplan
#   ./demo_start.sh airport --airport-code KJFK
#   ./demo_start.sh tower --airport-code KLAX
#   ./demo_start.sh center --center-id ZNY
#   ./demo_start.sh airplane --tail-number N338AA
#   ./demo_start.sh airplane --tail-number N338AA --wall-start-time 2026-10-05T21:14:03Z
#   ./demo_start.sh airplane --callsign UAL900 --from KORD --to KATL   (ad-hoc flight)
#   ./demo_start.sh tracon --tracon-id N90
#
set -eo pipefail

export RTI_MONITORING2_ENABLE=TRUE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_DIR="$REPO_DIR/python"
QOS_FILE="$REPO_DIR/air_traffic_qos.xml"

# ── Persistent local configuration ─────────────────────────────────────────
LOCAL_ENV_FILE="$REPO_DIR/.env.local"
if [[ -f "$LOCAL_ENV_FILE" ]]; then
    set -a
    source "$LOCAL_ENV_FILE"
    set +a
fi

# ── Connext license ────────────────────────────────────────────────────────
# NDDSHOME is used only to find a default license file. Do not point the
# native library path at $NDDSHOME/lib: the rti.connext wheel bundles its own
# libraries, and loading an installation's older ones breaks the import.
if [[ -n "${NDDSHOME:-}" ]]; then
    export RTI_LICENSE_FILE="${RTI_LICENSE_FILE:-$NDDSHOME/rti_license.dat}"
fi
# RTI_LICENSE_FILE can also be set directly without NDDSHOME
if [[ -z "${RTI_LICENSE_FILE:-}" ]]; then
    echo "ERROR: RTI_LICENSE_FILE is not set."
    echo "       Either set NDDSHOME or export RTI_LICENSE_FILE directly."
    exit 1
fi
if [[ ! -f "$RTI_LICENSE_FILE" ]]; then
    echo "ERROR: License file not found: $RTI_LICENSE_FILE"
    exit 1
fi

# ── QoS profiles visible to the default QosProvider ────────────────────────
# The Monitoring Library 2.0 resolves participant_qos_profile_name via the
# default/global QosProvider.  NDDS_QOS_PROFILES ensures our XML is loaded
# into that provider so the MonitoringUdpOnlyParticipant profile is found.
export NDDS_QOS_PROFILES="file://$QOS_FILE"

# ── Python from project venv ───────────────────────────────────────────────
PYTHON="${PYTHON:-$REPO_DIR/venv/bin/python3}"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python not found at $PYTHON"
    echo "       Run setup.sourceme first, or set PYTHON env var."
    exit 1
fi

DURATION=""
SCENARIO_CONFIG="$REPO_DIR/air_traffic_scenario.json"
WALL_START_TIME=""

# ── Helpers ─────────────────────────────────────────────────────────────────

usage() {
    sed -n '3,/^$/{ s/^# \?//; p }' "$0"
    exit 0
}

PIDS=()

cleanup() {
    echo ""
    echo "=== Shutting down all processes ==="
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null
    echo "All processes stopped."
}

wait_for_procs() {
    trap cleanup EXIT INT TERM
    echo ""
    echo "Press Ctrl+C to stop."
    wait
}

require_carto_api_key() {
    if [[ -z "${CARTO_BASEMAP_API_KEY:-}" ]]; then
        echo "ERROR: CARTO_BASEMAP_API_KEY is not set."
        echo "       Copy .env.example to .env.local and add your CARTO key."
        echo "       Request a key at https://carto.com/basemaps/apikey/"
        exit 1
    fi
}

# ── Individual launch functions ─────────────────────────────────────────────

start_flightplan() {
    local dur="$DURATION"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --duration) dur="$2"; shift 2 ;;
            *) shift ;;  # pass-through
        esac
    done
    echo "Starting Flight Plan Service (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_flightplan_service.py" --wall-start-time "$WALL_START_TIME" --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" --duration "$dur" &
    PIDS+=($!)
}

start_airport() {
    local code="KJFK" dur="$DURATION" extra_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --airport-code)    code="$2"; shift 2 ;;
            --duration)        dur="$2"; shift 2 ;;
            *)                 extra_args+=("$1"); shift ;;
        esac
    done
    echo "Starting Airport $code (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_airport.py" --wall-start-time "$WALL_START_TIME" \
        --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" \
        --airport-code "$code" --duration "$dur" "${extra_args[@]}" &
    PIDS+=($!)
}

start_tower() {
    local code="KJFK" dur="$DURATION" extra_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --airport-code)    code="$2"; shift 2 ;;
            --duration)        dur="$2"; shift 2 ;;
            *)                 extra_args+=("$1"); shift ;;
        esac
    done
    echo "Starting Tower $code (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_tower.py" --wall-start-time "$WALL_START_TIME" \
        --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" \
        --airport-code "$code" --duration "$dur" "${extra_args[@]}" &
    PIDS+=($!)
}

start_center() {
    local cid="ZNY" dur="$DURATION" extra_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --center-id) cid="$2"; shift 2 ;;
            --duration)  dur="$2"; shift 2 ;;
            *)           extra_args+=("$1"); shift ;;
        esac
    done
    echo "Starting Center $cid (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_center.py" --wall-start-time "$WALL_START_TIME" \
        --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" \
        --center-id "$cid" --duration "$dur" "${extra_args[@]}" &
    PIDS+=($!)
}

start_airplane() {
    local tail="" dur="$DURATION" extra_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --tail-number)  tail="$2"; shift 2 ;;
            --duration)     dur="$2"; shift 2 ;;
            *)              extra_args+=("$1"); shift ;;
        esac
    done
    echo "Starting Aircraft ${tail:-(ad-hoc)} (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_airplane.py" --wall-start-time "$WALL_START_TIME" \
        --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" \
        ${tail:+--tail-number "$tail"} --duration "$dur" "${extra_args[@]}" &
    PIDS+=($!)
}

start_tracon() {
    local tid="N90" dur="$DURATION" extra_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --tracon-id)       tid="$2"; shift 2 ;;
            --duration)        dur="$2"; shift 2 ;;
            *)                 extra_args+=("$1"); shift ;;
        esac
    done
    echo "Starting TRACON $tid (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_tracon.py" --wall-start-time "$WALL_START_TIME" \
        --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" \
        --tracon-id "$tid" --duration "$dur" "${extra_args[@]}" &
    PIDS+=($!)
}

start_dashboard() {
    local port="8050"
    require_carto_api_key
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --port) port="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    echo "Starting Dashboard (Flask on http://localhost:${port})..."
    "$PYTHON" "$PYTHON_DIR/app_dashboard.py" --wall-start-time "$WALL_START_TIME" --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" --port "$port" &
    PIDS+=($!)
}

start_weather() {
    local dur="$DURATION"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --duration)        dur="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    # Forecast and unforecast weather come from the scenario's "weather" block
    echo "Starting Weather Service (duration=${dur}s)..."
    "$PYTHON" "$PYTHON_DIR/app_weather_service.py" --wall-start-time "$WALL_START_TIME" \
        --config "$SCENARIO_CONFIG" --qos-file "$QOS_FILE" \
        --duration "$dur" &
    PIDS+=($!)
}

# ── "all" — full scenario ──────────────────────────────────────────────────

start_all() {
    require_carto_api_key
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --duration) DURATION="$2"; shift 2 ;;
            --config)   SCENARIO_CONFIG="$2"; shift 2 ;;
            *) echo "Unknown option: $1"; exit 1 ;;
        esac
    done

    if [[ ! -f "$SCENARIO_CONFIG" ]]; then
        echo "ERROR: Scenario config not found: $SCENARIO_CONFIG"
        exit 1
    fi

    # Query a single field from scenario config
    scenario_query() { "$PYTHON" "$SCRIPT_DIR/demo_cli.py" "$SCENARIO_CONFIG" "$1"; }

    local SCENARIO_NAME=$(scenario_query scenario)
    local CONFIG_DURATION=$(scenario_query duration)
    local AIRPORT_CODES=$(scenario_query airports)
    local TRACON_IDS=$(scenario_query tracons)
    local CENTER_IDS=$(scenario_query centers)
    local TAIL_NUMBERS=$(scenario_query aircraft)

    if [[ -z "$DURATION" ]]; then
        DURATION="$CONFIG_DURATION"
    fi

    echo "============================================"
    echo "  $SCENARIO_NAME"
    echo "  Config: $(basename "$SCENARIO_CONFIG")"
    echo "  Duration: ${DURATION}s"
    echo "  Wall start time: $WALL_START_TIME"
    echo "  (to add an app to this run: --wall-start-time $WALL_START_TIME)"
    echo "============================================"
    echo ""

    # 1. Flight Plan Service
    start_flightplan
    sleep 1

    # 2. Airports
    for code in $AIRPORT_CODES; do
        start_airport --airport-code "$code"
    done
    sleep 1

    # 3. Control Towers — one per airport
    for code in $AIRPORT_CODES; do
        start_tower --airport-code "$code"
    done
    sleep 1

    # 4. TRACONs
    for tid in $TRACON_IDS; do
        start_tracon --tracon-id "$tid"
    done
    sleep 1

    # 5. En-Route Centers
    for cid in $CENTER_IDS; do
        start_center --center-id "$cid"
    done
    sleep 1

    # 6. Weather Service
    start_weather
    sleep 1

    # 7. Aircraft
    for tail in $TAIL_NUMBERS; do
        start_airplane --tail-number "$tail"
        sleep 0.3
    done

    # 8. Dashboard
    sleep 2
    start_dashboard

    echo ""
    echo "=== All processes launched. Running for ${DURATION}s ==="
}

# ── Main dispatch ───────────────────────────────────────────────────────────

CMD="${1:-all}"
shift || true

# --wall-start-time applies to every command: take it out of the arguments
# and pass the same value to every app this script starts.
REMAINING_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wall-start-time) WALL_START_TIME="$2"; shift 2 ;;
        *) REMAINING_ARGS+=("$1"); shift ;;
    esac
done
set -- "${REMAINING_ARGS[@]}"
if [[ -z "$WALL_START_TIME" ]]; then
    WALL_START_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
# Single-app commands default to the scenario's duration as well
if [[ "$CMD" != "all" && -z "$DURATION" && -f "$SCENARIO_CONFIG" ]]; then
    DURATION="$("$PYTHON" "$SCRIPT_DIR/demo_cli.py" "$SCENARIO_CONFIG" duration)"
fi

case "$CMD" in
    all)         start_all "$@";               wait_for_procs ;;
    flightplan)  start_flightplan "$@";         wait_for_procs ;;
    airport)     start_airport "$@";            wait_for_procs ;;
    tower)       start_tower "$@";              wait_for_procs ;;
    tracon)      start_tracon "$@";             wait_for_procs ;;
    center)      start_center "$@";             wait_for_procs ;;
    airplane)    start_airplane "$@";           wait_for_procs ;;
    dashboard)   start_dashboard "$@";          wait_for_procs ;;
    weather)     start_weather "$@";            wait_for_procs ;;
    help|-h|--help) usage ;;
    *) echo "Unknown command: $CMD"; echo "Run '$0 help' for usage."; exit 1 ;;
esac
