#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f local/runtime.env ]]; then source local/runtime.env; fi
systemctl --user start magic-video-api.service magic-video-web.service
if systemctl --user cat magic-video-mcp.service >/dev/null 2>&1; then
  systemctl --user start magic-video-mcp.service
fi
for attempt in {1..60}; do
  if curl -fsS --max-time 2 http://127.0.0.1:5173/studio/projects >/dev/null 2>&1; then
    echo 'Magic Studio: http://127.0.0.1:5173'
    echo "ComfyUI starts on demand: ${MAGIC_DESKTOP_COMFY_URL:-http://localhost:8188} (${MAGIC_DESKTOP_COMFY_PLATFORM:-wsl})"
    exit 0
  fi
  sleep 1
done
echo 'Studio did not become ready; see local/api.log and local/web.log.' >&2
exit 1
