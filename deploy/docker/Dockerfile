# A slim image that runs "automation schedule" (the in-process scheduler) as a non-root user.
# Build from the project root:   docker build -f deploy/docker/Dockerfile -t automation .
# Or use docker-compose.yml:      docker compose up -d --build
# config.toml is mounted at /app/config.toml; secrets come from the environment (env_file in compose).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Pinned dependencies first, so a code change does not reinstall them.
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-deps . \
    && useradd --create-home --uid 10001 automation \
    && mkdir -p /app/state \
    && chown automation:automation /app/state

USER automation
VOLUME ["/app/state"]

# SIGTERM (docker stop) lets running jobs finish; give the container enough stop time (compose: stop_grace_period).
CMD ["automation", "schedule"]
