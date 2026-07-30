#!/bin/sh
set -eu
ENV_FILE=/etc/tsingpaws-agent.env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
export LAUNCHER_UPSTREAM="${LAUNCHER_UPSTREAM:-http://127.0.0.1:18880}"
export BRIDGE_LISTEN_HOST="${BRIDGE_LISTEN_HOST:-0.0.0.0}"
export BRIDGE_LISTEN_PORT="${BRIDGE_LISTEN_PORT:-18800}"
export AGENT_STATUS_BASE="${AGENT_STATUS_BASE:-http://127.0.0.1:18791}"
export BRIDGE_STATIC_DIR="${BRIDGE_STATIC_DIR:-/opt/tsingpaws-agent/static}"
export DEVICE_ID="${DEVICE_ID:-home-001}"
exec /usr/bin/python3 /opt/tsingpaws-agent/launcher_bridge.py
