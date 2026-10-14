# fluent-monitor-web only (no Qt): numpy + smbprotocol, about 80 MB on top of the base image.
# Built by docker/docker-compose.yml; see docker/README.md.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/data

WORKDIR /app
# dependencies first (cached layer), exactly the versions in uv.lock
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --only-group server --no-install-project
COPY src ./src

VOLUME /data
EXPOSE 8050
ENTRYPOINT ["python", "-m", "fluent_monitor.web"]
