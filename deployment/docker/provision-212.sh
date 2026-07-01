#!/usr/bin/env sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 执行本脚本。" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  openssh-client \
  sshfs \
  docker.io

if apt-cache show docker-compose-plugin >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends docker-compose-plugin
  COMPOSE_LABEL="docker compose plugin"
elif apt-cache show docker-compose >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends docker-compose
  COMPOSE_LABEL="docker-compose"
else
  echo "未找到 docker compose 可安装包，请检查当前 Debian 软件源。" >&2
  exit 1
fi

mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "live-restore": true,
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "5"
  }
}
EOF

systemctl enable --now docker

mkdir -p \
  /srv/safetyraise/nginx \
  /srv/safetyraise/letsencrypt/conf \
  /srv/safetyraise/letsencrypt/www \
  /srv/safetyraise/kbase \
  /srv/safetyraise/runtime \
  /srv/safetyraise/models \
  /srv/safetyraise/huggingface

echo "212 基础环境已准备完成：docker / ${COMPOSE_LABEL} / sshfs / 目录骨架 / Docker 日志上限。"
