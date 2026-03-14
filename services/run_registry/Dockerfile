FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.7.22 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-dev

COPY app ./app
COPY migrations ./migrations

EXPOSE 8080

CMD ["sh", "-c", "uv run uvicorn app.main:app --host ${RUN_REGISTRY_HOST:-0.0.0.0} --port ${RUN_REGISTRY_PORT:-8080}"]
