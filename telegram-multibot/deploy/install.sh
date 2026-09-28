#!/bin/sh

# Reversible OpenWrt installer for one PicoClaw Gateway with multiple
# independent Telegram bot accounts.

set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SELF_DIR/.." && pwd)
ADAPTER_SOURCE="$ROOT_DIR/backend/telegram_multibot_adapter.py"
ADAPTER_INIT_SOURCE="$SELF_DIR/tsingpaws-telegram-multibot.init"
BRIDGE_SOURCE="$ROOT_DIR/integration/launcher_bridge.py"
BRIDGE_WRAPPER_SOURCE="$ROOT_DIR/integration/run-bridge.sh"
BRIDGE_INIT_SOURCE="$ROOT_DIR/integration/tsingpaws-bridge.init"
STATIC_SOURCE="$ROOT_DIR/integration/static"

ADAPTER_DIR=/opt/tsingpaw/extensions/telegram-multibot
ADAPTER_TARGET="$ADAPTER_DIR/telegram_multibot_adapter.py"
CONFIG_TARGET=/opt/tsingpaw/data/telegram-bots.json
ADAPTER_INIT=/etc/init.d/tsingpaws-telegram-multibot
TSINGPAW_CONF=/etc/tsingpaw.conf
TSINGPAW_INIT=/etc/init.d/tsingpaw
BRIDGE_DIR=/opt/tsingpaws-agent
BRIDGE_STATIC="$BRIDGE_DIR/static"
BRIDGE_SCRIPT="$BRIDGE_DIR/launcher_bridge.py"
BRIDGE_WRAPPER="$BRIDGE_DIR/run-bridge.sh"
BRIDGE_INIT=/etc/init.d/tsingpaws-bridge
BACKUP_ROOT=/opt/tsingpaw/backups
STATE_FILE="$ADAPTER_DIR/install-state"
MANAGED_BEGIN='# BEGIN TSINGPAWS TELEGRAM MULTIBOT'
MANAGED_END='# END TSINGPAWS TELEGRAM MULTIBOT'
FIREWALL_SECTION=tsingpaws_multibot_private_launcher
STATIC_FILES='cloud-channel.css cloud-channel.js skill-library.css skill-library.js telegram-multibot.css telegram-multibot.js'

say() { printf '%s\n' "telegram-multibot install: $*"; }
die() { say "ERROR: $*" >&2; exit 1; }

service_running() {
	[ -x "/etc/init.d/$1" ] && "/etc/init.d/$1" running >/dev/null 2>&1
}

wait_http() {
	_url="$1"
	_attempt=0
	while [ "$_attempt" -lt 30 ]; do
		if command -v curl >/dev/null 2>&1; then
			curl -fsS --max-time 2 "$_url" >/dev/null 2>&1 && return 0
		else
			wget -q -T 2 -O /dev/null "$_url" >/dev/null 2>&1 && return 0
		fi
		_attempt=$((_attempt + 1))
		sleep 1
	done
	return 1
}

backup_one() {
	_target="$1"
	_name="$2"
	[ ! -e "$_target" ] || cp -p "$_target" "$BACKUP_DIR/files/$_name"
}

restore_one() {
	_name="$1"
	_target="$2"
	_mode="$3"
	if [ -e "$BACKUP_DIR/files/$_name" ]; then
		cp -p "$BACKUP_DIR/files/$_name" "$_target"
	else
		rm -f "$_target"
	fi
	[ ! -e "$_target" ] || chmod "$_mode" "$_target"
}

restore_from_backup() {
	[ -n "${BACKUP_DIR:-}" ] || return 0
	case "$BACKUP_DIR" in
		/opt/tsingpaw/backups/telegram-multibot-*) ;;
		*) say "refusing unsafe backup path: $BACKUP_DIR" >&2; return 1 ;;
	esac
	[ -d "$BACKUP_DIR/files" ] || return 1

	[ ! -x "$ADAPTER_INIT" ] || "$ADAPTER_INIT" stop >/dev/null 2>&1 || true
	[ ! -x "$BRIDGE_INIT" ] || "$BRIDGE_INIT" stop >/dev/null 2>&1 || true
	/etc/init.d/tsingpaw stop >/dev/null 2>&1 || true
	if [ "${FIREWALL_RULE_CREATED:-0}" = 1 ]; then
		uci -q delete "firewall.$FIREWALL_SECTION" || true
		uci commit firewall
		/etc/init.d/firewall reload >/dev/null 2>&1 || true
	fi

	restore_one tsingpaw.init "$TSINGPAW_INIT" 755
	restore_one adapter.init "$ADAPTER_INIT" 755
	restore_one adapter.py "$ADAPTER_TARGET" 600
	restore_one rollback.sh "$ADAPTER_DIR/rollback.sh" 700
	restore_one verify.sh "$ADAPTER_DIR/verify.sh" 700
	restore_one install-state "$STATE_FILE" 600
	restore_one bridge.init "$BRIDGE_INIT" 755
	restore_one bridge.py "$BRIDGE_SCRIPT" 600
	restore_one bridge-wrapper.sh "$BRIDGE_WRAPPER" 755
	for _asset in $STATIC_FILES; do
		restore_one "static-$_asset" "$BRIDGE_STATIC/$_asset" 644
	done

	if [ -e "$BACKUP_DIR/files/tsingpaw.conf" ]; then
		cp -p "$BACKUP_DIR/files/tsingpaw.conf" "$TSINGPAW_CONF"
	else
		[ -e "$TSINGPAW_CONF" ] || : >"$TSINGPAW_CONF"
		awk -v begin="$MANAGED_BEGIN" -v end="$MANAGED_END" '
			$0 == begin { skip=1; next }
			$0 == end { skip=0; next }
			!skip { print }
		' "$TSINGPAW_CONF" >"$TSINGPAW_CONF.rollback"
		mv "$TSINGPAW_CONF.rollback" "$TSINGPAW_CONF"
		chmod 600 "$TSINGPAW_CONF"
	fi
	if [ "${HAD_CONFIG:-0}" = 1 ] && [ -e "$CONFIG_TARGET" ]; then
		case "${CONFIG_MODE:-}" in
			[0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) chmod "$CONFIG_MODE" "$CONFIG_TARGET" ;;
		esac
	fi

	if [ "${BRIDGE_WAS_ENABLED:-0}" = 1 ] && [ -x "$BRIDGE_INIT" ]; then "$BRIDGE_INIT" enable >/dev/null 2>&1 || true; fi
	if [ "${BRIDGE_WAS_ENABLED:-0}" = 0 ] && [ -x "$BRIDGE_INIT" ]; then "$BRIDGE_INIT" disable >/dev/null 2>&1 || true; fi
	if [ "${ADAPTER_WAS_ENABLED:-0}" = 1 ] && [ -x "$ADAPTER_INIT" ]; then "$ADAPTER_INIT" enable >/dev/null 2>&1 || true; fi
	if [ "${ADAPTER_WAS_ENABLED:-0}" = 0 ] && [ -x "$ADAPTER_INIT" ]; then "$ADAPTER_INIT" disable >/dev/null 2>&1 || true; fi
	[ "${TSINGPAW_WAS_RUNNING:-1}" = 1 ] && /etc/init.d/tsingpaw start >/dev/null 2>&1 || true
	[ "${BRIDGE_WAS_RUNNING:-0}" = 1 ] && [ -x "$BRIDGE_INIT" ] && "$BRIDGE_INIT" start >/dev/null 2>&1 || true
	[ "${ADAPTER_WAS_RUNNING:-0}" = 1 ] && [ -x "$ADAPTER_INIT" ] && "$ADAPTER_INIT" start >/dev/null 2>&1 || true
}

failed() {
	_rc=$?
	trap - EXIT HUP INT TERM
	say "deployment failed; restoring original files and service state" >&2
	restore_from_backup || say "automatic restore was incomplete; backup: ${BACKUP_DIR:-unknown}" >&2
	exit "$_rc"
}

[ "$(id -u)" = 0 ] || die "must run as root"
[ -x /usr/bin/python3 ] || die "missing /usr/bin/python3"
[ -x "$TSINGPAW_INIT" ] || die "TsingPaws is not installed: $TSINGPAW_INIT"
command -v awk >/dev/null 2>&1 || die "missing awk"
command -v uci >/dev/null 2>&1 || die "missing uci (OpenWrt is required)"
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "curl or wget is required"
for _source in "$ADAPTER_SOURCE" "$ADAPTER_INIT_SOURCE" "$BRIDGE_SOURCE" "$BRIDGE_WRAPPER_SOURCE" "$BRIDGE_INIT_SOURCE" "$SELF_DIR/rollback.sh" "$SELF_DIR/verify.sh"; do
	[ -r "$_source" ] || die "missing package file: $_source"
done
for _asset in $STATIC_FILES; do
	[ -r "$STATIC_SOURCE/$_asset" ] || die "missing UI asset: $_asset"
done

mkdir -p "$BACKUP_ROOT" "$ADAPTER_DIR" /opt/tsingpaw/data "$BRIDGE_STATIC"
umask 077
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="$BACKUP_ROOT/telegram-multibot-$STAMP-$$"
mkdir -p "$BACKUP_DIR/files"

backup_one "$ADAPTER_INIT" adapter.init
backup_one "$ADAPTER_TARGET" adapter.py
backup_one "$ADAPTER_DIR/rollback.sh" rollback.sh
backup_one "$ADAPTER_DIR/verify.sh" verify.sh
backup_one "$STATE_FILE" install-state
backup_one "$BRIDGE_INIT" bridge.init
backup_one "$BRIDGE_SCRIPT" bridge.py
backup_one "$BRIDGE_WRAPPER" bridge-wrapper.sh
backup_one "$TSINGPAW_CONF" tsingpaw.conf
backup_one "$TSINGPAW_INIT" tsingpaw.init
for _asset in $STATIC_FILES; do backup_one "$BRIDGE_STATIC/$_asset" "static-$_asset"; done

HAD_CONFIG=0
CONFIG_MODE=600
if [ -e "$CONFIG_TARGET" ]; then
	HAD_CONFIG=1
	CONFIG_MODE=$(stat -c '%a' "$CONFIG_TARGET" 2>/dev/null || echo 600)
	cp -p "$CONFIG_TARGET" "$BACKUP_DIR/telegram-bots.json"
fi
BRIDGE_WAS_RUNNING=0; ADAPTER_WAS_RUNNING=0; TSINGPAW_WAS_RUNNING=0
BRIDGE_WAS_ENABLED=0; ADAPTER_WAS_ENABLED=0; FIREWALL_RULE_CREATED=0
service_running tsingpaws-bridge && BRIDGE_WAS_RUNNING=1 || true
service_running tsingpaws-telegram-multibot && ADAPTER_WAS_RUNNING=1 || true
service_running tsingpaw && TSINGPAW_WAS_RUNNING=1 || true
[ -x "$BRIDGE_INIT" ] && "$BRIDGE_INIT" enabled >/dev/null 2>&1 && BRIDGE_WAS_ENABLED=1 || true
[ -x "$ADAPTER_INIT" ] && "$ADAPTER_INIT" enabled >/dev/null 2>&1 && ADAPTER_WAS_ENABLED=1 || true
uci -q get "firewall.$FIREWALL_SECTION" >/dev/null 2>&1 || FIREWALL_RULE_CREATED=1

cat >"$BACKUP_DIR/state" <<EOF
BACKUP_DIR='$BACKUP_DIR'
HAD_CONFIG='$HAD_CONFIG'
CONFIG_MODE='$CONFIG_MODE'
BRIDGE_WAS_RUNNING='$BRIDGE_WAS_RUNNING'
ADAPTER_WAS_RUNNING='$ADAPTER_WAS_RUNNING'
TSINGPAW_WAS_RUNNING='$TSINGPAW_WAS_RUNNING'
BRIDGE_WAS_ENABLED='$BRIDGE_WAS_ENABLED'
ADAPTER_WAS_ENABLED='$ADAPTER_WAS_ENABLED'
FIREWALL_RULE_CREATED='$FIREWALL_RULE_CREATED'
EOF
chmod 600 "$BACKUP_DIR/state"
trap failed EXIT HUP INT TERM

install_file() {
	_source="$1"; _target="$2"; _mode="$3"
	cp "$_source" "$_target.new"
	chmod "$_mode" "$_target.new"
	mv "$_target.new" "$_target"
}

install_file "$ADAPTER_SOURCE" "$ADAPTER_TARGET" 600
install_file "$ADAPTER_INIT_SOURCE" "$ADAPTER_INIT" 755
install_file "$BRIDGE_SOURCE" "$BRIDGE_SCRIPT" 600
install_file "$BRIDGE_WRAPPER_SOURCE" "$BRIDGE_WRAPPER" 755
install_file "$BRIDGE_INIT_SOURCE" "$BRIDGE_INIT" 755
for _asset in $STATIC_FILES; do install_file "$STATIC_SOURCE/$_asset" "$BRIDGE_STATIC/$_asset" 644; done
install_file "$SELF_DIR/rollback.sh" "$ADAPTER_DIR/rollback.sh" 700
install_file "$SELF_DIR/verify.sh" "$ADAPTER_DIR/verify.sh" 700
/usr/bin/python3 -m py_compile "$ADAPTER_TARGET" "$BRIDGE_SCRIPT"

if [ ! -e "$CONFIG_TARGET" ]; then printf '%s\n' '{"version":1,"bots":[]}' >"$CONFIG_TARGET"; fi
chmod 600 "$CONFIG_TARGET"
/usr/bin/python3 -m json.tool "$CONFIG_TARGET" >/dev/null

[ -e "$TSINGPAW_CONF" ] || : >"$TSINGPAW_CONF"
awk -v begin="$MANAGED_BEGIN" -v end="$MANAGED_END" '
	$0 == begin { skip=1; next }
	$0 == end { skip=0; next }
	!skip { print }
' "$TSINGPAW_CONF" >"$TSINGPAW_CONF.new"
cat >>"$TSINGPAW_CONF.new" <<EOF
$MANAGED_BEGIN
TSINGPAW_HTTP_PORT=18880
TSINGPAW_PUBLIC=0
$MANAGED_END
EOF
chmod 600 "$TSINGPAW_CONF.new"
mv "$TSINGPAW_CONF.new" "$TSINGPAW_CONF"

if grep -q '^EXTRA_CMD_ARGS="-console -no-browser -public"$' "$TSINGPAW_INIT"; then
	sed 's/^EXTRA_CMD_ARGS="-console -no-browser -public"$/EXTRA_CMD_ARGS="-console -no-browser"\n[ "${TSINGPAW_PUBLIC:-1}" = "1" ] \&\& EXTRA_CMD_ARGS="$EXTRA_CMD_ARGS -public"/' "$TSINGPAW_INIT" >"$TSINGPAW_INIT.new"
	chmod 755 "$TSINGPAW_INIT.new"
	mv "$TSINGPAW_INIT.new" "$TSINGPAW_INIT"
fi
grep -q 'TSINGPAW_PUBLIC:-1' "$TSINGPAW_INIT" || die "unsupported TsingPaws init script; cannot make 18880 private"

uci set "firewall.$FIREWALL_SECTION=rule"
uci set "firewall.$FIREWALL_SECTION.name=TsingPaws private launcher 18880"
uci set "firewall.$FIREWALL_SECTION.src=*"
uci set "firewall.$FIREWALL_SECTION.proto=tcp"
uci set "firewall.$FIREWALL_SECTION.dest_port=18880"
uci set "firewall.$FIREWALL_SECTION.target=REJECT"
uci set "firewall.$FIREWALL_SECTION.family=any"
uci commit firewall
/etc/init.d/firewall reload >/dev/null 2>&1

cp "$BACKUP_DIR/state" "$STATE_FILE.new"
chmod 600 "$STATE_FILE.new"
mv "$STATE_FILE.new" "$STATE_FILE"

"$BRIDGE_INIT" stop >/dev/null 2>&1 || true
/etc/init.d/tsingpaw restart >/dev/null 2>&1
wait_http http://127.0.0.1:18880/ || die "native launcher did not become healthy on 18880"
wait_http http://127.0.0.1:18790/health || die "existing Gateway is not healthy on 18790"
"$ADAPTER_INIT" enable
"$ADAPTER_INIT" restart >/dev/null 2>&1
wait_http http://127.0.0.1:18792/health || die "Telegram adapter did not become healthy on 18792"
"$BRIDGE_INIT" enable
"$BRIDGE_INIT" restart >/dev/null 2>&1
wait_http http://127.0.0.1:18800/ || die "unified bridge did not become healthy on 18800"

trap - EXIT HUP INT TERM
say "installed successfully; backup: $BACKUP_DIR"
say "manage bots at http://DEVICE_IP:18800/telegram-bots"
say "verify: $ADAPTER_DIR/verify.sh"
say "rollback: $ADAPTER_DIR/rollback.sh"
