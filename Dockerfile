# syntax=docker/dockerfile:1
FROM node:24-slim AS web-build
WORKDIR /web
# Empty by default (Sentry disabled in the browser build). Vite embeds these
# VITE_-prefixed vars into the static bundle at build time; see
# web/src/monitoring.ts, web/src/analytics.ts and cloudbuild.yaml's
# substitutions. An empty VITE_CF_BEACON_TOKEN leaves Cloudflare Web Analytics off.
ARG VITE_SENTRY_DSN=""
ARG VITE_SENTRY_ENVIRONMENT="production"
ARG VITE_APP_RELEASE=""
ARG VITE_CF_BEACON_TOKEN=""
ENV VITE_SENTRY_DSN=${VITE_SENTRY_DSN} \
    VITE_SENTRY_ENVIRONMENT=${VITE_SENTRY_ENVIRONMENT} \
    VITE_APP_RELEASE=${VITE_APP_RELEASE} \
    VITE_CF_BEACON_TOKEN=${VITE_CF_BEACON_TOKEN}
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system worker \
    && useradd --system --gid worker --create-home worker

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY scripts/build_worker_package.py ./scripts/build_worker_package.py
RUN python scripts/build_worker_package.py
# The public downloads (MCP server wheel, agent skill) are built from source here, not
# committed, and ship as package data served under /studio/downloads.
COPY mcp/pyproject.toml mcp/README.md mcp/LICENSE ./mcp/
COPY mcp/src ./mcp/src
COPY mcp/skill ./mcp/skill
COPY scripts/build_downloads.py ./scripts/build_downloads.py
RUN python scripts/build_downloads.py
RUN pip install --no-cache-dir .

# Target adapters are separate packages (see docs/superpowers/specs/
# 2026-09-02-target-adapter-interface.md), published to a private Artifact
# Registry Python repository and installed here under the build's existing
# Google Cloud identity (the same one `gcloud auth configure-docker` already
# authenticates before the image build). TARGET_ADAPTER_PACKAGES is a
# space-separated list of pip requirement specs; the public default is empty
# so the stock image ships only the neutral example adapter and this step is
# skipped entirely. The access token arrives only as a BuildKit secret mount
# (never a build arg, never a layer, never on the pip command line) and is
# read from the mount file rather than the environment. `pip check` proves
# the adapter's own `magic-video-editor` requirement was satisfied by the
# source install above rather than pulling a second copy.
ARG TARGET_ADAPTER_PACKAGES=""
ARG TARGET_ADAPTER_INDEX_URL=""
RUN --mount=type=secret,id=ar_token \
    if [ -n "$TARGET_ADAPTER_PACKAGES" ]; then \
        PIP_EXTRA_INDEX_URL="https://oauth2accesstoken:$(cat /run/secrets/ar_token)@${TARGET_ADAPTER_INDEX_URL#https://}" \
        pip install --no-cache-dir $TARGET_ADAPTER_PACKAGES \
        && pip check; \
    fi

# Extensions (src/magic_video_editor/extensions.py) that live beside the source in
# a private checkout: EXTENSION_PATHS lists their directories in the build context,
# each named after the extension it registers. Each is installed from the context
# without dependencies (it runs on the Studio installed above) and named in
# MAGIC_VIDEO_EDITOR_EXTENSIONS, so the service refuses to start without it. The
# public default is empty: nothing is installed or required.
ARG EXTENSION_PATHS=""
RUN --mount=type=bind,target=/context,rw \
    for path in $EXTENSION_PATHS; do \
        pip install --no-cache-dir --no-deps "/context/$path" || exit 1; \
    done
ENV MAGIC_VIDEO_EDITOR_EXTENSIONS=${EXTENSION_PATHS}

USER worker
EXPOSE 8080

CMD ["uvicorn", "magic_video_editor.app:app", "--host", "0.0.0.0", "--port", "8080"]
