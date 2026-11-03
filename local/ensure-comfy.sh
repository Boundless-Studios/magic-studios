#!/usr/bin/env bash
set -euo pipefail
if [[ ${MAGIC_DESKTOP_COMFY_PLATFORM:-wsl} == windows ]]; then
  if curl -fsS --max-time 3 http://127.0.0.1:8189/system_stats >/dev/null 2>&1; then exit 0; fi
  attention=${MAGIC_DESKTOP_GLOBAL_ATTENTION:-${MAGIC_DESKTOP_ATTENTION:-pytorch}}
  if [[ $attention == ck ]]; then attention=pytorch; fi
  exec /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\AI\start-windows-comfy.ps1' -Attention "$attention"
fi
systemctl --user start --no-block magic-video-comfy.service
