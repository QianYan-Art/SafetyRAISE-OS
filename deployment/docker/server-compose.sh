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

exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
