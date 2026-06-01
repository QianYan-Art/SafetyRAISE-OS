#!/usr/bin/env sh
set -eu

REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_HOST="${REMOTE_HOST:-<DATA_SERVER_IP>}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_PATH="${REMOTE_PATH:-/srv/safetyraise-data/kbase}"
LOCAL_PATH="${LOCAL_PATH:-/srv/safetyraise/kbase}"
IDENTITY_FILE="${IDENTITY_FILE:-/root/.ssh/<DATA_SERVER_IP>_ssh.key}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 执行本脚本。" >&2
  exit 1
fi

if ! command -v sshfs >/dev/null 2>&1; then
  echo "未找到 sshfs，请先执行 deployment/docker/provision-212.sh。" >&2
  exit 1
fi

if [ ! -f "$IDENTITY_FILE" ]; then
  echo "缺少 SSH 私钥：$IDENTITY_FILE" >&2
  exit 1
fi

mkdir -p /root/.ssh "$LOCAL_PATH"

if mountpoint -q "$LOCAL_PATH"; then
  echo "知识库挂载点已存在：$LOCAL_PATH"
  exit 0
fi

ssh-keyscan -p "$REMOTE_PORT" -H "$REMOTE_HOST" >> /root/.ssh/known_hosts 2>/dev/null || true

sshfs \
  -o IdentityFile="$IDENTITY_FILE" \
  -o StrictHostKeyChecking=accept-new \
  -o reconnect \
  -o ro \
  -o port="$REMOTE_PORT" \
  "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH}" \
  "$LOCAL_PATH"

echo "已挂载知识库目录：${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PORT}:${REMOTE_PATH} -> ${LOCAL_PATH}"
