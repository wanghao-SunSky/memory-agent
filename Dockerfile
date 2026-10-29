FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/*
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/models
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY migrations ./migrations
COPY scripts/container_entrypoint.sh ./scripts/container_entrypoint.sh
RUN useradd --uid 10001 --create-home app && mkdir /models && chown -R app:app /models /app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT ["/app/scripts/container_entrypoint.sh"]
CMD ["api"]
