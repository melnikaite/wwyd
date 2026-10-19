# syntax=docker/dockerfile:1
# wwyd container image: docker run -v wwyd:/data -p 8740:8740 <image>

# --- build stage: resolve and install dependencies and the project with uv ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, so source edits do not invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime stage: plain slim Python, no uv, no build tools ---
FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.title="wwyd" \
      org.opencontainers.image.source="https://github.com/melnikaite/wwyd" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
RUN groupadd --system --gid 10001 wwyd \
 && useradd --system --uid 10001 --gid wwyd --home-dir /data --shell /usr/sbin/nologin wwyd \
 && mkdir -p /data && chown wwyd:wwyd /data && chmod 700 /data
COPY --from=build --chown=wwyd:wwyd /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    WWYD_DATA_DIR=/data \
    WWYD_HOST=0.0.0.0 \
    WWYD_PORT=8740
USER wwyd
VOLUME ["/data"]
EXPOSE 8740
# /healthz answers once the app (and its DB migrations) are up; no DB writes, not throttled.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('WWYD_PORT','8740')}/healthz\", timeout=4)"]
CMD ["wwyd", "serve"]
