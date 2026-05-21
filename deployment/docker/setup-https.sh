#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$PROJECT_ROOT/.env.server"
COMPOSE_SCRIPT="$SCRIPT_DIR/server-compose.sh"
HTTP_CONF_SRC="$SCRIPT_DIR/nginx.frontend.http.conf"
HTTPS_CONF_SRC="$SCRIPT_DIR/nginx.frontend.https.conf"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE，请先从 .env.example 复制并填写。" >&2
  exit 1
fi

set -a
. "$ENV_FILE"
set +a

PRIMARY_DOMAIN=${LETSENCRYPT_PRIMARY_DOMAIN:-example.com}
DOMAINS_CSV=${LETSENCRYPT_DOMAINS:-$PRIMARY_DOMAIN}
EMAIL=${LETSENCRYPT_EMAIL:-}
NGINX_HOST_PATH=${FRONTEND_NGINX_HOST_PATH:-/srv/safetyraise/nginx}
LETSENCRYPT_CONF=${LETSENCRYPT_CONF_HOST_PATH:-/srv/safetyraise/letsencrypt/conf}
LETSENCRYPT_WWW=${LETSENCRYPT_WWW_HOST_PATH:-/srv/safetyraise/letsencrypt/www}
CRON_FILE=/etc/cron.d/safetyraise-cert-renew
FRONTEND_WAIT_ATTEMPTS=${FRONTEND_WAIT_ATTEMPTS:-30}
FRONTEND_WAIT_SECONDS=${FRONTEND_WAIT_SECONDS:-1}

is_frontend_running() {
  docker ps --filter "name=^/docker-frontend-1$" --filter "status=running" -q | grep -q .
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
  if ! wait_for_frontend_running; then
    return 1
  fi
  docker exec docker-frontend-1 nginx -s reload
}

mkdir -p "$NGINX_HOST_PATH" "$LETSENCRYPT_CONF" "$LETSENCRYPT_WWW"
cp "$HTTP_CONF_SRC" "$NGINX_HOST_PATH/default.conf"

echo "已写入 HTTP 校验配置，准备启动前端容器。"
sh "$COMPOSE_SCRIPT" up -d --build frontend

set -- certonly --webroot -w /var/www/certbot --non-interactive --agree-tos --keep-until-expiring
OLD_IFS=$IFS
IFS=','
for domain in $DOMAINS_CSV; do
  if [ -n "$domain" ]; then
    set -- "$@" -d "$domain"
  fi
done
IFS=$OLD_IFS

if [ -n "$EMAIL" ]; then
  set -- "$@" --email "$EMAIL"
else
  set -- "$@" --register-unsafely-without-email
fi

echo "开始向 Let's Encrypt 申请证书。"
docker run --rm \
  -v "$LETSENCRYPT_CONF:/etc/letsencrypt" \
  -v "$LETSENCRYPT_WWW:/var/www/certbot" \
  certbot/certbot:latest \
  "$@"

cp "$HTTPS_CONF_SRC" "$NGINX_HOST_PATH/default.conf"
echo "证书申请成功，切换 HTTPS 配置。"
sh "$COMPOSE_SCRIPT" up -d frontend
reload_frontend_nginx

cat > "$CRON_FILE" <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3,15 * * * root cd $PROJECT_ROOT && sh deployment/docker/renew-https.sh >> /var/log/safetyraise-cert-renew.log 2>&1
EOF
chmod 644 "$CRON_FILE"

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now cron >/dev/null 2>&1 || true
fi

echo "HTTPS 已启用，自动续期任务已写入 $CRON_FILE。"
