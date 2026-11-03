#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/magic-video-mcp.service" <<UNIT
[Unit]
Description=Magic Studios MCP for loopback and SSH tunnel development
After=magic-video-api.service magic-video-web.service
Wants=magic-video-api.service magic-video-web.service

[Service]
WorkingDirectory=$project_root
Environment=MAGIC_STUDIOS_URL=http://127.0.0.1:5173
Environment=MAGIC_STUDIOS_PUBLIC_URL=${MAGIC_STUDIOS_PUBLIC_URL:-http://127.0.0.1:5173}
Environment=MAGIC_STUDIOS_MCP_TRANSPORT=streamable-http
Environment=MAGIC_STUDIOS_MCP_HOST=127.0.0.1
Environment=MAGIC_STUDIOS_MCP_PORT=8766
ExecStart=$project_root/mcp/.venv/bin/magic-video-studio-mcp
Restart=on-failure
RestartSec=3
StandardOutput=append:$project_root/local/mcp-http.log
StandardError=append:$project_root/local/mcp-http.log

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now magic-video-mcp.service
