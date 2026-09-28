#!/bin/sh

set -eu

ADAPTER_DIR=/opt/tsingpaw/extensions/telegram-multibot
STATE_FILE="$ADAPTER_DIR/install-state"
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
MANAGED_BEGIN='# BEGIN TSINGPAWS TELEGRAM MULTIBOT'
MANAGED_END='# END TSINGPAWS TELEGRAM MULTIBOT'
FIREWALL_SECTION=tsingpaws_multibot_private_launcher
STATIC_FILES='cloud-channel.css cloud-channel.js skill-library.css skill-library.js telegram-multibot.css telegram-multibot.js'

say() { printf '%s\n' "telegram-multibot rollback: $*"; }
die() { say "ERROR: $*" >&2; exit 1; }

restore_one() {
	_name="$1"; _target="$2"; _mode="$3"
	if [ -e "$BACKUP_DIR/files/$_name" ]; then
		cp -p "$BACKUP_DIR/files/$_name" "$_target"
	else
		rm -f "$_target"
	fi
	[ ! -e "$_target" ] || chmod "$_mode" "$_target"
}

[ "$(id -u)" = 0 ] || die "must run as root"
[ -r "$STATE_FILE" ] || die "missing rollback state: $STATE_FILE"
. "$STATE_FILE"
case "${BACKUP_DIR:-}" in
	/opt/tsingpaw/backups/telegram-multibot-*) ;;
	*) die "unsafe backup path in state file" ;;
esac
[ -d "$BACKUP_DIR/files" ] || die "backup directory is missing: $BACKUP_DIR"

[ ! -x "$ADAPTER_INIT" ] || "$ADAPTER_INIT" disable >/dev/null 2>&1 || true
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
restore_one bridge.init "$BRIDGE_INIT" 755
restore_one bridge.py "$BRIDGE_SCRIPT" 600
restore_one bridge-wrapper.sh "$BRIDGE_WRAPPER" 755
for _asset in $STATIC_FILES; do restore_one "static-$_asset" "$BRIDGE_STATIC/$_asset" 644; done

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

# Keep the current bot list: users may have added bots after installation.
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

# Restore any previous extension utilities/state last. On a first installation
# this removes only package-owned helper files; bot data and backups stay.
restore_one verify.sh "$ADAPTER_DIR/verify.sh" 700
restore_one install-state "$STATE_FILE" 600
restore_one rollback.sh "$ADAPTER_DIR/rollback.sh" 700

say "original files and service state restored from $BACKUP_DIR"
say "telegram-bots.json and backup data were retained"
