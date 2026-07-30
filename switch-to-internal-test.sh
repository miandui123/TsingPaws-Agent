#!/bin/sh
# Switch this device from the single-node Agent to the internal-test Agent.
# Refuses to proceed unless the public Relay is already a supported internal-test build.
# Never prints tokens. Restores single_node on any failure.
set -u

AGENT_DIR=/opt/tsingpaws-agent
CONF_DIR=/etc/tsingpaws-agent
ENV_FILE=/etc/tsingpaws-agent.env
MODE_FILE="$CONF_DIR/mode"
DEVICE_FILE="$CONF_DIR/device.json"
ENROLL_FILE="$CONF_DIR/enrollment.env"
INIT=/etc/init.d/tsingpaws-agent
PY=/usr/bin/python3

say() { printf '%s\n' "$*"; }
fail() { say "失败: $*"; exit 1; }

run_agent() {
	# shellcheck disable=SC1090
	( set -a; . "$ENV_FILE"; set +a; \
	  AGENT_CONF_DIR="$CONF_DIR" STATUS_PORT="${STATUS_PORT:-18791}" \
	  "$PY" "$AGENT_DIR/agent.py" "$@" )
}

restore_single_node() {
	say "回滚到单机版…"
	printf 'single_node\n' > "$MODE_FILE"
	chmod 0644 "$MODE_FILE" 2>/dev/null
	"$INIT" restart >/dev/null 2>&1
	sleep 3
	say "已恢复 single_node"
}

mkdir -p "$CONF_DIR" || fail "无法创建 $CONF_DIR"
chmod 0700 "$CONF_DIR"

say "1/8 检查公网 Relay 版本…"
run_agent check-health || fail "公网 Relay 尚未升级到内部测试版（internal-test-2 / internal-test-auth-1），已停止，未做任何改动"

say "2/8 检查注册凭证文件…"
[ -f "$ENROLL_FILE" ] || fail "缺少 $ENROLL_FILE"
PERM=$(stat -c '%a %U:%G' "$ENROLL_FILE" 2>/dev/null)
case "$PERM" in
	"600 root:root") : ;;
	*) fail "$ENROLL_FILE 权限必须为 0600 root:root（当前 $PERM）" ;;
esac

say "3/8 停止单机版 Agent…"
"$INIT" stop >/dev/null 2>&1
sleep 2

say "4/8 执行内部测试版注册…"
if ! run_agent register; then
	rc=$?
	say "注册未成功（退出码 $rc）"
	restore_single_node
	exit "$rc"
fi

say "5/8 校验设备身份文件…"
[ -f "$DEVICE_FILE" ] || { say "缺少 $DEVICE_FILE"; restore_single_node; exit 1; }
DPERM=$(stat -c '%a %U:%G' "$DEVICE_FILE" 2>/dev/null)
[ "$DPERM" = "600 root:root" ] || { say "$DEVICE_FILE 权限异常（$DPERM）"; restore_single_node; exit 1; }
"$PY" - "$DEVICE_FILE" <<'PY' || { say "device.json 内容异常"; restore_single_node; exit 1; }
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(d.get("device_id"), str) and d["device_id"]
assert isinstance(d.get("device_token"), str) and len(d["device_token"]) >= 16
compact = d["device_id"].replace("-", "")
print("device_id_short:", compact[:8] + "…" + compact[-4:])
PY

say "6/8 切换运行模式为 internal_test…"
printf 'internal_test\n' > "$MODE_FILE"
chmod 0644 "$MODE_FILE"

say "7/8 启动内部测试版 Agent…"
"$INIT" start >/dev/null 2>&1
sleep 3

say "8/8 校验 Relay 连接…"
OK=0
i=0
while [ "$i" -lt 12 ]; do
	if "$PY" - <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:18791/status", timeout=3) as r:
        d = json.load(r)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("mode") == "internal_test" and d.get("registered") and d.get("relay_connected") else 1)
PY
	then OK=1; break; fi
	i=$((i+1))
	sleep 3
done

if [ "$OK" != "1" ]; then
	say "内部测试版未能连上 Relay"
	restore_single_node
	exit 1
fi

say "完成: 已切换到内部测试版并连接成功。回滚请执行 $AGENT_DIR/switch-to-single-node.sh"
exit 0
