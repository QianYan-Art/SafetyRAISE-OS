# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ARG INSTALL_VIDEO_DEPS=false

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
COPY backend/requirements-video.txt /app/backend/requirements-video.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip \
    && python -m pip install -r /app/backend/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "${INSTALL_VIDEO_DEPS}" = "true" ]; then python -m pip install -r /app/backend/requirements-video.txt; fi

COPY backend /app/backend
COPY deployment/docker/backend-entrypoint.sh /usr/local/bin/backend-entrypoint.sh

RUN chmod +x /usr/local/bin/backend-entrypoint.sh

WORKDIR /app/backend

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/backend-entrypoint.sh"]
CMD ["python", "-m", "app.main", "serve", "--host", "0.0.0.0", "--port", "8000", "--config", "config/workflow.server.yaml"]
