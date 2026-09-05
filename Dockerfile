FROM python:3.12-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /opt/awc
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 awc \
    && useradd --uid 10001 --gid awc --no-create-home --home-dir /tmp/awc awc \
    && mkdir -p /data/coordinator /data/worker /run/config \
    && chown -R awc:awc /data

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY deploy/healthcheck.py ./deploy/healthcheck.py
RUN python -m pip install --no-cache-dir '.[langgraph]'

EXPOSE 8080
USER awc:awc
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
    CMD ["python", "/opt/awc/deploy/healthcheck.py", "api"]
CMD ["python", "-m", "agentic_workflow.server", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/coordinator/coordinator.db", "--auth-file", "/run/config/auth.json"]

# Build explicitly with --target rpa, or use deploy/compose.browser.yaml.
FROM runtime-base AS rpa
USER root
RUN python -m pip install --no-cache-dir '.[rpa]' \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*
USER awc:awc

# Keep the default image small; browser dependencies are opt-in.
FROM runtime-base AS runtime
