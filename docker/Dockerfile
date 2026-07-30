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
    DATABASE_PATH=/appdata/aurorabot.db

# Create a non-root user, plus the dirs it will own. Both are created
# explicitly: /appdata sits outside WORKDIR so it needs its own chown, and /app
# does not exist yet at this point — the WORKDIR instruction below is what would
# otherwise create it, which is too late for the chown here.
RUN groupadd --gid 1000 aurora \
    && useradd --uid 1000 --gid aurora --shell /bin/bash --create-home aurora \
    && mkdir -p /app /appdata \
    && chown -R aurora:aurora /app /appdata

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

# Persist SQLite DB + cached logos on the mounted volume.
#
# NOTE: because this VOLUME is declared, Docker creates an *anonymous* volume
# when no bind mount is supplied rather than failing — so a container started
# without the /appdata mapping writes happily to a directory that never
# appears on the host, and loses it whenever the container is recreated.
# Always map /appdata to a host path (Unraid: /mnt/user/appdata/aurorabot).
VOLUME ["/appdata"]

EXPOSE 8080

# Ping the internal status flag; unhealthy after ~3 failed checks.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["python", "-u", "-m", "src.bot"]
