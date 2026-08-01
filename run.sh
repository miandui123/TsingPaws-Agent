#!/bin/sh
set -eu
ENV_FILE=/etc/tsingpaws-agent.env
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export PICO_WS_PATH="${PICO_WS_PATH:-/pico/ws}"
export STATUS_PORT="${STATUS_PORT:-18791}"
export AGENT_CONF_DIR="${AGENT_CONF_DIR:-/etc/tsingpaws-agent}"
export AGENT_DATA_DIR="${AGENT_DATA_DIR:-/opt/tsingpaws-agent-data}"
exec /usr/bin/python3 /opt/tsingpaws-agent/agent.py
