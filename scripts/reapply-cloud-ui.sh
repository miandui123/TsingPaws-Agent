#!/bin/sh
# Re-apply TsingPaws cloud UI bridge after PicoClaw/launcher upgrades.
set -eu
ROOT=/opt/tsingpaws-agent
SRC=${1:-/opt/tsingpaws-agent/upgrade-assets}

cp -f "$SRC/agent.py" "$ROOT/agent.py"
cp -f "$SRC/launcher_bridge.py" "$ROOT/launcher_bridge.py"
cp -f "$SRC/run.sh" "$ROOT/run.sh"
cp -f "$SRC/run-bridge.sh" "$ROOT/run-bridge.sh"
mkdir -p "$ROOT/static"
cp -f "$SRC/static/"* "$ROOT/static/" 2>/dev/null || true
chmod 0755 "$ROOT/agent.py" "$ROOT/run.sh" "$ROOT/run-bridge.sh" "$ROOT/launcher_bridge.py"

# Keep launcher behind local port; public entry remains 18800 via bridge.
cat >/etc/tsingpaw.conf <<'EOF'
# Managed by tsingpaws-agent cloud bridge
TSINGPAW_HTTP_PORT=18880
TSINGPAW_PUBLIC=0
EOF

# Ensure tsingpaw init honors TSINGPAW_PUBLIC if not already patched
if ! grep -q 'TSINGPAW_PUBLIC' /etc/init.d/tsingpaw; then
  echo "WARNING: /etc/init.d/tsingpaw missing TSINGPAW_PUBLIC support; restore from backup/patch." >&2
fi

/etc/init.d/tsingpaws-agent restart || /etc/init.d/tsingpaws-agent start
/etc/init.d/tsingpaws-bridge enable
/etc/init.d/tsingpaws-bridge restart || /etc/init.d/tsingpaws-bridge start
echo "reapplied ok"
