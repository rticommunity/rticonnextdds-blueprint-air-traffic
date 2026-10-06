#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Run the ATC demo image using repository-local configuration.
#
# Usage:
#   ./docker/run.sh              # full demo
#   ./docker/run.sh dashboard    # a single component
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_DIR/.env.local"
IMAGE="${ATC_DOCKER_IMAGE:-atc-demo}"
LOCAL_DIR="$SCRIPT_DIR/.local"
STAGED_LICENSE="$LOCAL_DIR/rti_license.dat"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: Local configuration not found: $ENV_FILE"
    echo "       Copy .env.example to .env.local and replace the placeholders."
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

if [[ -z "${RTI_LICENSE_FILE:-}" ]]; then
    echo "ERROR: RTI_LICENSE_FILE is not set in $ENV_FILE."
    exit 1
fi
if [[ ! -r "$RTI_LICENSE_FILE" ]]; then
    echo "ERROR: RTI license file not found or not readable: $RTI_LICENSE_FILE"
    exit 1
fi

# Docker Desktop may not share the license's original host directory
# (for example, /Applications on macOS). Copy the file—not a symlink—to
# ignored repository-local state, which Docker can already access.
umask 077
mkdir -p "$LOCAL_DIR"
chmod 700 "$LOCAL_DIR"
cp -fL "$RTI_LICENSE_FILE" "$STAGED_LICENSE"
chmod 600 "$STAGED_LICENSE"

exec docker run --rm \
    -e CARTO_BASEMAP_API_KEY \
    -e DOMAIN_ID \
    -e DOMAIN_TAG \
    -p 8050:8050 \
    -v "$STAGED_LICENSE:/tmp/rti_license.dat:ro" \
    "$IMAGE" "$@"