#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$PROJECT_ROOT/.env.server"
COMPOSE_FILE="$PROJECT_ROOT/deployment/docker/docker-compose.server.yml"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE，请先从 .env.example 复制并填写。" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
fi

if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
fi

echo "未找到可用的 Docker Compose，请先安装 docker compose plugin 或 docker-compose。" >&2
exit 1
