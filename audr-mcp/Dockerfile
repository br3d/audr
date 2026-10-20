# Image for the opt-in streamable-HTTP transport only (compose.mcp.yaml).
# The recommended path — stdio, launched locally by Claude Desktop / Claude
# Code — needs no container at all: `pip install audr-mcp` or `uvx audr-mcp`
# on the operator's own machine. See docs/mcp.md.
FROM python:3.14-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.14-slim AS runtime
WORKDIR /app

RUN useradd -m -u 1000 -s /bin/bash audr-mcp

COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/

RUN chown -R audr-mcp:audr-mcp /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

USER audr-mcp

EXPOSE 8800
CMD ["python", "-m", "audr_mcp", "--transport", "http"]
