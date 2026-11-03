#!/usr/bin/env bash
set -euo pipefail
if systemctl --user cat magic-video-mcp.service >/dev/null 2>&1; then
  systemctl --user stop magic-video-mcp.service
fi
systemctl --user stop magic-video-api.service magic-video-web.service magic-video-comfy.service
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\AI\stop-windows-comfy.ps1'
