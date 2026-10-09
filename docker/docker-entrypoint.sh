#!/bin/bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Docker entrypoint for the ATC demo.
#
# The host-side run.sh helper loads .env.local, passes the CARTO key, and
# stages the license identified by RTI_LICENSE_FILE under docker/.local
# before mounting it at /tmp/rti_license.dat.
#
# Usage:
#   ./docker/run.sh
#   ./docker/run.sh dashboard
set -euo pipefail

# ── Connext license ───────────────────────────────────────────────────────
# Prefer the standard container mount. This intentionally replaces the
# host-only RTI_LICENSE_FILE path loaded from .env.local.
if [[ -f /tmp/rti_license.dat ]]; then
    export RTI_LICENSE_FILE=/tmp/rti_license.dat
elif [[ -z "${RTI_LICENSE_FILE:-}" ]]; then
    echo "ERROR: RTI license file not found."
    echo "       Use docker/run.sh so RTI_LICENSE_FILE is mounted from .env.local."
    exit 1
fi

# ── Delegate to the demo launcher ─────────────────────────────────────────
export PYTHON="${PYTHON:-$(command -v python3)}"
exec /app/scripts/demo_start.sh "$@"
