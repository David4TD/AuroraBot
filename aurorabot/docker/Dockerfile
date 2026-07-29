# ─── AuroraBot multi-stage image ────────────────────────────────────────────
# Stage 1: build wheels in a full image so the runtime stays slim.
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ─── Stage 2: runtime ───────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEALTH_PORT=8080 \
    DATABASE_PATH=/app/data/aurorabot.db

# Create a non-root user and the data dir it will own.
RUN groupadd --gid 1000 aurora \
    && useradd --uid 1000 --gid aurora --shell /bin/bash --create-home aurora \
    && mkdir -p /app/data \
    && chown -R aurora:aurora /app

WORKDIR /app

# Install deps from the pre-built wheels (no compiler needed here).
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Application source.
COPY --chown=aurora:aurora src ./src
COPY --chown=aurora:aurora docker/healthcheck.py ./healthcheck.py

USER aurora

# Persist SQLite DB + logs on the mounted volume.
VOLUME ["/app/data"]

EXPOSE 8080

# Ping the internal status flag; unhealthy after ~3 failed checks.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["python", "-u", "-m", "src.bot"]
