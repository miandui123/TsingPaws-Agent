#!/bin/sh

# Public bootstrap installer. Runtime files are fetched from an immutable tag
# and verified before the device installer is executed.

set -eu

VERSION=v0.1.0
RAW_BASE="https://raw.githubusercontent.com/miandui123/TsingPaws-Telegram-Multibot/$VERSION"
MANIFEST=checksums-runtime.sha256
MANIFEST_SHA256=cae54e19f72ae134dd1175800edc80c5f30b06850590916e9c2e380f4f49e860

say() { printf '%s\n' "tsingpaws-multibot bootstrap: $*"; }
die() { say "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "must run as root"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v awk >/dev/null 2>&1 || die "awk is required"
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "curl or wget is required"

TMP_DIR=$(mktemp -d /tmp/tsingpaws-multibot.XXXXXX)
cleanup() {
	case "$TMP_DIR" in /tmp/tsingpaws-multibot.*) rm -rf "$TMP_DIR" ;; esac
}
trap cleanup EXIT HUP INT TERM

download() {
	_url="$1"; _target="$2"
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL --connect-timeout 15 --max-time 120 "$_url" -o "$_target"
	else
		wget -q -T 120 -O "$_target" "$_url"
	fi
}

say "downloading verified payload $VERSION"
download "$RAW_BASE/$MANIFEST" "$TMP_DIR/$MANIFEST"
_actual_manifest=$(sha256sum "$TMP_DIR/$MANIFEST" | awk '{print $1}')
[ "$_actual_manifest" = "$MANIFEST_SHA256" ] || die "manifest checksum mismatch"

while read -r _expected _relative; do
	[ -n "${_expected:-}" ] || continue
	case "${_relative:-}" in
		backend/telegram_multibot_adapter.py | \
		deploy/install.sh | deploy/rollback.sh | deploy/verify.sh | deploy/tsingpaws-telegram-multibot.init | \
		integration/launcher_bridge.py | integration/run-bridge.sh | integration/tsingpaws-bridge.init | \
		integration/static/cloud-channel.css | integration/static/cloud-channel.js | \
		integration/static/skill-library.css | integration/static/skill-library.js | \
		integration/static/telegram-multibot.css | integration/static/telegram-multibot.js) ;;
		*) die "unexpected path in manifest: ${_relative:-empty}" ;;
	esac
	case "$_expected" in *[!0-9a-f]*|'') die "invalid checksum in manifest" ;; esac
	[ "${#_expected}" = 64 ] || die "invalid checksum length in manifest"
	mkdir -p "$TMP_DIR/${_relative%/*}"
	download "$RAW_BASE/$_relative" "$TMP_DIR/$_relative"
	_actual=$(sha256sum "$TMP_DIR/$_relative" | awk '{print $1}')
	[ "$_actual" = "$_expected" ] || die "checksum mismatch: $_relative"
done <"$TMP_DIR/$MANIFEST"

chmod 700 "$TMP_DIR/deploy/install.sh" "$TMP_DIR/deploy/rollback.sh" "$TMP_DIR/deploy/verify.sh"
sh "$TMP_DIR/deploy/install.sh"
