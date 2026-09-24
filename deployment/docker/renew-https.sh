#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
# 发布目录部署时脚本单独安装在发布目录之外（如 /srv/safetyraise/bin），没有 .env.server，
# 各字段取下方默认值；仓库检出部署仍读取项目根目录的 .env.server。
ENV_FILE="${SAFETYRAISE_ENV_FILE:-$PROJECT_ROOT/.env.server}"
COMPOSE_SCRIPT="$SCRIPT_DIR/server-compose.sh"

read_env_value() {
  key="$1"
  default_value="${2:-}"
  value=
  if [ -f "$ENV_FILE" ]; then
    value=$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1)
  fi
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$default_value"
  fi
}

LETSENCRYPT_CONF=$(read_env_value "LETSENCRYPT_CONF_HOST_PATH" "/srv/safetyraise/letsencrypt/conf")
LETSENCRYPT_WWW=$(read_env_value "LETSENCRYPT_WWW_HOST_PATH" "/srv/safetyraise/letsencrypt/www")
FRONTEND_WAIT_ATTEMPTS=$(read_env_value "FRONTEND_WAIT_ATTEMPTS" "30")
FRONTEND_WAIT_SECONDS=$(read_env_value "FRONTEND_WAIT_SECONDS" "1")

get_frontend_container_id() {
  docker ps \
    --filter "label=com.docker.compose.service=frontend" \
    --filter "status=running" \
    -q \
  | head -n 1
}

is_frontend_running() {
  [ -n "$(get_frontend_container_id)" ]
}

wait_for_frontend_running() {
  attempt=1
  while [ "$attempt" -le "$FRONTEND_WAIT_ATTEMPTS" ]; do
    if is_frontend_running; then
      return 0
    fi
    sleep "$FRONTEND_WAIT_SECONDS"
    attempt=$((attempt + 1))
  done
  echo "frontend 容器未在预期时间内进入运行态。" >&2
  return 1
}

reload_frontend_nginx() {
  frontend_container_id=
  if ! is_frontend_running; then
    if [ ! -f "$ENV_FILE" ] || [ ! -f "$COMPOSE_SCRIPT" ]; then
      echo "frontend 容器未运行且无编排配置；证书已续期，容器下次启动时加载。"
      return 0
    fi
    echo "frontend 容器当前未运行，尝试先拉起。"
    sh "$COMPOSE_SCRIPT" up -d frontend
  fi
  if ! wait_for_frontend_running; then
    return 1
  fi
  frontend_container_id=$(get_frontend_container_id)
  if [ -z "$frontend_container_id" ]; then
    echo "未找到 frontend 运行容器，无法热重载 Nginx。" >&2
    return 1
  fi
  docker exec "$frontend_container_id" nginx -t
  docker exec "$frontend_container_id" nginx -s reload
}

mkdir -p "$LETSENCRYPT_CONF" "$LETSENCRYPT_WWW"

docker run --rm \
  -v "$LETSENCRYPT_CONF:/etc/letsencrypt" \
  -v "$LETSENCRYPT_WWW:/var/www/certbot" \
  certbot/certbot:latest \
  renew --webroot -w /var/www/certbot --quiet

reload_frontend_nginx
