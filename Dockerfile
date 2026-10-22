# fluent-monitor (numpy + smbprotocol), about 80 MB on top of the base image.
# Built by docker/docker-compose.yml; see docker/README.md.
# Debian 13 "trixie": bookworm only gets LTS (reduced) security support since July 2026.
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/data \
    FM_BIND=0.0.0.0 \
    FM_NO_BROWSER=1

# security updates of the base system that came out after the base image was built
RUN if command -v apt-get >/dev/null; then \
        apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*; fi

WORKDIR /app
# dependencies first (cached layer), exactly the versions in uv.lock
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project
COPY src ./src

# run as Unraid's nobody:users, not as root; /data holds the access token and settings
RUN python -c "import os; os.makedirs('/data', exist_ok=True); os.chown('/data', 99, 100)"
USER 99:100

VOLUME /data
EXPOSE 8050
ENTRYPOINT ["python", "-m", "fluent_monitor.web"]
