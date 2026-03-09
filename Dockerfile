FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README_IMPLEMENTATION.md ./
COPY app ./app

RUN uv sync --frozen --no-dev

COPY openapi.full.yaml openapi.actions.yaml README.md ARCHITECTURE.md TEST_PLAN.md ./
COPY scripts ./scripts

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
