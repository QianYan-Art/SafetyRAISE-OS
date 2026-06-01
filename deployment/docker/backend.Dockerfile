FROM rust:1.95-slim AS rust-token-accel-builder

WORKDIR /build/query_token_accel
COPY backend/native/query_token_accel/Cargo.toml /build/query_token_accel/Cargo.toml
COPY backend/native/query_token_accel/src /build/query_token_accel/src
RUN cargo build --release

FROM python:3.12-slim

ARG INSTALL_VIDEO_DEPS=false

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend
ENV PIP_NO_CACHE_DIR=1
ENV SAFETYRAISE_TOKEN_ACCEL_LIB=/app/backend/native/query_token_accel/libquery_token_accel.so

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
COPY backend/requirements-video.txt /app/backend/requirements-video.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/backend/requirements.txt

RUN if [ "${INSTALL_VIDEO_DEPS}" = "true" ]; then \
      python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
      && python -m pip install --no-cache-dir -r /app/backend/requirements-video.txt; \
    fi

COPY backend /app/backend
COPY --from=rust-token-accel-builder /build/query_token_accel/target/release/libquery_token_accel.so /app/backend/native/query_token_accel/libquery_token_accel.so
COPY deployment/docker/backend-entrypoint.sh /usr/local/bin/backend-entrypoint.sh

RUN chmod +x /usr/local/bin/backend-entrypoint.sh

WORKDIR /app/backend

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/backend-entrypoint.sh"]
CMD ["python", "-m", "app.main", "serve", "--host", "0.0.0.0", "--port", "8000", "--config", "config/workflow.server.yaml"]
