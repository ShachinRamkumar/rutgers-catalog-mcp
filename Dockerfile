# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS runtime

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
