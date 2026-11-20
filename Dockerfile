# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.13-slim-bookworm AS runtime

RUN groupadd --system mcp && useradd --system --gid mcp --create-home mcp

WORKDIR /app
COPY --from=builder --chown=mcp:mcp /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    RUTGERS_SOC_CACHE_DIR="/tmp/rutgers-catalog-mcp/soc" \
    NJ_TRANSFER_CACHE_DIR="/tmp/rutgers-catalog-mcp/nj-transfer"

USER mcp

# All three stdio servers are installed on PATH:
#   rutgers-soc-mcp (default), middlesex-courses-mcp, nj-transfer-mcp
CMD ["rutgers-soc-mcp"]
