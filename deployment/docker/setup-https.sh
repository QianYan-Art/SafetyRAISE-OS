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

read_env_value() {
  key="$1"
  default_value="${2:-}"
  value=$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1)
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$default_value"
  fi
}

PRIMARY_DOMAIN=$(read_env_value "LETSENCRYPT_PRIMARY_DOMAIN" "example.com")
DOMAINS_CSV=$(read_env_value "LETSENCRYPT_DOMAINS" "$PRIMARY_DOMAIN")
EMAIL=$(read_env_value "LETSENCRYPT_EMAIL" "")
NGINX_HOST_PATH=$(read_env_value "FRONTEND_NGINX_HOST_PATH" "/srv/safetyraise/nginx")
LETSENCRYPT_CONF=$(read_env_value "LETSENCRYPT_CONF_HOST_PATH" "/srv/safetyraise/letsencrypt/conf")
LETSENCRYPT_WWW=$(read_env_value "LETSENCRYPT_WWW_HOST_PATH" "/srv/safetyraise/letsencrypt/www")
CRON_FILE=/etc/cron.d/safetyraise-cert-renew
LOGROTATE_FILE=/etc/logrotate.d/safetyraise-cert-renew
FRONTEND_WAIT_ATTEMPTS=$(read_env_value "FRONTEND_WAIT_ATTEMPTS" "30")
FRONTEND_WAIT_SECONDS=$(read_env_value "FRONTEND_WAIT_SECONDS" "1")
DOMAINS_SPACE=$(printf '%s' "$DOMAINS_CSV" | tr ',' ' ' | xargs)

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
  if ! wait_for_frontend_running; then
    return 1
  fi
  frontend_container_id=$(get_frontend_container_id)
  if [ -z "$frontend_container_id" ]; then
    echo "未找到 frontend 运行容器，无法热重载 Nginx。" >&2
    return 1
  fi
  docker exec "$frontend_container_id" nginx -s reload
}

render_http_conf() {
  sed \
    -e 's/server_name example.com;/server_name '"${DOMAINS_SPACE}"';/' \
    -e 's/return 301 https:\/\/example.com\$request_uri;/return 301 https:\/\/'"${PRIMARY_DOMAIN}"'\$request_uri;/' \
    "$HTTP_CONF_SRC" > "$NGINX_HOST_PATH/default.conf"
}

render_https_conf() {
  sed \
    -e 's/server_name example.com;/server_name '"${DOMAINS_SPACE}"';/' \
    -e 's/return 301 https:\/\/example.com\$request_uri;/return 301 https:\/\/'"${PRIMARY_DOMAIN}"'\$request_uri;/' \
    -e 's#/etc/letsencrypt/live/example.com/#/etc/letsencrypt/live/'"${PRIMARY_DOMAIN}"'/#g' \
    "$HTTPS_CONF_SRC" > "$NGINX_HOST_PATH/default.conf"
}

mkdir -p "$NGINX_HOST_PATH" "$LETSENCRYPT_CONF" "$LETSENCRYPT_WWW"
render_http_conf

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

render_https_conf
echo "证书申请成功，切换 HTTPS 配置。"
sh "$COMPOSE_SCRIPT" up -d frontend
reload_frontend_nginx

cat > "$CRON_FILE" <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3,15 * * * root cd $PROJECT_ROOT && sh deployment/docker/renew-https.sh >> /var/log/safetyraise-cert-renew.log 2>&1
EOF
chmod 644 "$CRON_FILE"

cat > "$LOGROTATE_FILE" <<'EOF'
/var/log/safetyraise-cert-renew.log {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    create 0640 root adm
}
EOF
chmod 644 "$LOGROTATE_FILE"

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now cron >/dev/null 2>&1 || true
fi

echo "HTTPS 已启用，自动续期任务已写入 $CRON_FILE，日志轮转规则已写入 $LOGROTATE_FILE。"
