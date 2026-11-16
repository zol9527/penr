FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* README.md LICENSE ./
COPY schema.sql ./
COPY src ./src
COPY skills ./skills

RUN uv sync --frozen --no-dev || uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV ANTIFOX_HOST=0.0.0.0
ENV ANTIFOX_PORT=8787

EXPOSE 8787
CMD ["penr", "serve", "--host", "0.0.0.0", "--port", "8787"]
