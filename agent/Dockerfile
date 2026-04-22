# syntax=docker/dockerfile:1.6
#
# Multi-stage image for the Python orchestrator.
#
#   Stage 1: copy the source + lockfile and materialise a uv-managed venv
#            (no dev extras — the runtime image shouldn't carry pytest/ruff).
#   Stage 2: slim runtime that only contains the venv + the package source.
#
# Build:  docker build -t registry.localhost:5000/devops-agent:latest agent
# Run:    docker run --rm -p 8000:8000 registry.localhost:5000/devops-agent:latest

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# `uv` is installed from its official distroless image and copied over as a
# static binary — avoids pulling build-essential / pip just to bootstrap it.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /build

# Resolve deps first (no source) so the layer caches across source edits.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 1000 agent \
 && useradd  --system --uid 1000 --gid agent --home /home/agent --create-home agent

COPY --from=builder --chown=agent:agent /opt/venv /opt/venv
COPY --from=builder --chown=agent:agent /build/src /app/src

WORKDIR /app
USER agent

EXPOSE 8000

# FastAPI intake on 0.0.0.0:8000. No --reload in the image; that's dev-only.
CMD ["python", "-m", "agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
