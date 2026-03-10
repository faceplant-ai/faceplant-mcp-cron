FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends cron && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash cron

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project
COPY src/ ./src/

COPY config/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
