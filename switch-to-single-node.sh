#!/bin/sh
# Roll the Agent back to the single-node protocol.
# device.json is kept on purpose, so a later switch does not re-register.
set -u

CONF_DIR=/etc/tsingpaws-agent
MODE_FILE="$CONF_DIR/mode"
INIT=/etc/init.d/tsingpaws-agent
PY=/usr/bin/python3

say() { printf '%s\n' "$*"; }

mkdir -p "$CONF_DIR"
printf 'single_node\n' > "$MODE_FILE"
chmod 0644 "$MODE_FILE"
say "已写入 mode=single_node（device.json 保留，未删除）"

"$INIT" restart >/dev/null 2>&1
sleep 3

i=0
while [ "$i" -lt 10 ]; do
	if "$PY" - <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:18791/status", timeout=3) as r:
        d = json.load(r)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("mode") == "single_node" and d.get("relay_connected") else 1)
PY
	then say "完成: 单机版 Agent 已连接 Relay"; exit 0; fi
	i=$((i+1))
	sleep 3
done

say "警告: 已切回 single_node，但尚未确认 Relay 连接，请检查 /etc/init.d/tsingpaws-agent status"
exit 1
