#!/usr/bin/env bash
# Bind development UI/MCP to loopback; access from another computer through SSH.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
override_dir="$HOME/.config/systemd/user/magic-video-web.service.d"
mkdir -p "$override_dir"
cat > "$override_dir/loopback.conf" <<UNIT
[Service]
ExecStart=
ExecStart=/usr/bin/env node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort
UNIT
MAGIC_STUDIOS_PUBLIC_URL=http://127.0.0.1:5173 "$project_root/local/install-mcp-service.sh"
systemctl --user daemon-reload
systemctl --user restart magic-video-web.service magic-video-mcp.service
echo 'Studio 127.0.0.1:5173 and MCP 127.0.0.1:8766; use SSH forwarding from the Mac.'
