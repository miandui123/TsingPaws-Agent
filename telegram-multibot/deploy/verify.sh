#!/bin/sh

set -u

CONFIG=/opt/tsingpaw/data/telegram-bots.json
FAILURES=0

ok() { printf 'OK   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }

http_check() {
	_name="$1"
	_url="$2"
	if command -v curl >/dev/null 2>&1; then
		curl -fsS --max-time 3 "$_url" >/dev/null 2>&1
	elif command -v wget >/dev/null 2>&1; then
		wget -q -T 3 -O /dev/null "$_url" >/dev/null 2>&1
	else
		return 1
	fi
}

port_check() {
	_port="$1"
	_port_hex=$(printf '%04X' "$_port")
	awk -v port=":$_port_hex" '$2 ~ port"$" && $4 == "0A" { found=1 } END { exit !found }' \
		/proc/net/tcp /proc/net/tcp6 2>/dev/null
}

loopback_only() {
	_port="$1"
	_port_hex=$(printf '%04X' "$_port")
	awk -v port=":$_port_hex" '
		$2 ~ port"$" && $4 == "0A" {
			found=1
			if ($2 !~ /^(0100007F|00000000000000000000000001000000):/) bad=1
		}
		END { exit (!found || bad) }
	' /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

process_check() {
	_pattern="$1"
	if command -v pgrep >/dev/null 2>&1; then
		pgrep -f "$_pattern" >/dev/null 2>&1
	else
		ps w | grep -F "$_pattern" | grep -v grep >/dev/null 2>&1
	fi
}

for port in 18800 18790 18792; do
	port_check "$port" && ok "port $port is listening" || bad "port $port is not listening"
done
loopback_only 18792 && ok "adapter port 18792 is loopback-only" || bad "adapter port 18792 is missing or exposed beyond loopback"
if loopback_only 18880; then
	ok "native launcher port 18880 is loopback-only"
elif [ "$(uci -q get firewall.tsingpaws_multibot_private_launcher.src)" = "*" ] && \
	[ "$(uci -q get firewall.tsingpaws_multibot_private_launcher.dest_port)" = "18880" ] && \
	[ "$(uci -q get firewall.tsingpaws_multibot_private_launcher.target)" = "REJECT" ]; then
	ok "native launcher port 18880 is protected by the all-zone firewall rule"
else
	bad "native launcher port 18880 is exposed beyond loopback"
fi

http_check bridge http://127.0.0.1:18800/ && ok "18800 unified entry responds" || bad "18800 unified entry health failed"
http_check gateway http://127.0.0.1:18790/health && ok "18790 Gateway health responds" || bad "18790 Gateway health failed"
http_check adapter http://127.0.0.1:18792/health && ok "18792 adapter health responds" || bad "18792 adapter health failed"
http_check launcher http://127.0.0.1:18880/ && ok "18880 native launcher responds" || bad "18880 native launcher health failed"

process_check telegram_multibot_adapter.py && ok "Telegram adapter process exists" || bad "Telegram adapter process missing"
process_check launcher_bridge.py && ok "launcher bridge process exists" || bad "launcher bridge process missing"
process_check 'picoclaw gateway' && ok "existing Gateway process exists" || bad "existing Gateway process missing"

if PICO_BASE_URL=ws://127.0.0.1:18880 \
	PICO_VIA_LAUNCHER=1 \
	PICO_SECURITY_FILE=/opt/tsingpaw/data/.security.yml \
	PICO_LAUNCHER_CONFIG=/opt/tsingpaw/data/launcher-config.json \
	/usr/bin/python3 -S - <<'PY'
import asyncio
import importlib.util
import sys

path = "/opt/tsingpaw/extensions/telegram-multibot/telegram_multibot_adapter.py"
spec = importlib.util.spec_from_file_location("telegram_multibot_verify", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

async def verify():
    session = module.PicoSession(None, "verify", "health")
    await session.ensure()
    await session.close()

asyncio.run(verify())
PY
then
	ok "adapter authenticated Pico WebSocket handshake succeeds"
else
	bad "adapter cannot authenticate a Pico WebSocket session"
fi

if [ -r "$CONFIG" ]; then
	_mode=$(stat -c '%a' "$CONFIG" 2>/dev/null || echo unknown)
	case "$_mode" in
		600|400) ok "Telegram config permissions are $_mode" ;;
		*) bad "Telegram config permissions are $_mode; expected 600 or 400" ;;
	esac

	if command -v logread >/dev/null 2>&1; then
		if /usr/bin/python3 - "$CONFIG" <<'PY'
import json
import re
import subprocess
import sys

config_path = sys.argv[1]
try:
    with open(config_path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    log_text = subprocess.check_output(
        ["logread"], stderr=subprocess.DEVNULL, text=True, errors="replace"
    )
except Exception:
    raise SystemExit(2)

tokens = []
def visit(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"token", "bot_token"} and isinstance(item, str) and item:
                tokens.append(item)
            else:
                visit(item)
    elif isinstance(value, list):
        for item in value:
            visit(item)

visit(document)
literal_leak = any(token in log_text for token in tokens)
shape_leak = bool(re.search(r"(?<![A-Za-z0-9_])\d{6,12}:[A-Za-z0-9_-]{20,}", log_text))
raise SystemExit(1 if literal_leak or shape_leak else 0)
PY
		then
			ok "no Telegram token found in system log"
		else
			_rc=$?
			[ "$_rc" = 2 ] && bad "could not parse Telegram config for leak scan" || bad "Telegram token-like secret found in system log"
		fi
	else
		bad "logread is unavailable; token leak scan not performed"
	fi
else
	bad "Telegram config is missing or unreadable"
fi

[ "$FAILURES" -eq 0 ] || exit 1
ok "all deployment checks passed"
