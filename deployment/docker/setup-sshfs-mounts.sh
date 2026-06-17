#!/usr/bin/env sh
# 在应用服务器上把数据服务器的 kbase/runtime sshfs 挂载固化为 systemd 单元，
# 实现「开机自启 + 重启自愈」，根治应用服务器重启后挂载丢失、backend 绑定空目录的问题。
#
# 设计要点：
#   1. 两个 systemd .mount 单元（Type=fuse.sshfs，_netdev，开机自启）——重启后自动挂载；
#   2. 一个 oneshot 服务 safetyraise-backend-bindrefresh.service——在网络/docker/挂载
#      就绪后 `docker restart` backend，让容器重新识别 bind mount（容器 bind 在挂载就绪前
#      启动会捕获空目录，必须在挂载之后刷新）；
#   3. 不用 RequiresMountsFor 去 gate docker.service，避免数据服务器不可达时把整个
#      应用服务器（含前端/登录）一起拖垮——前端保持可用，仅知识库相关功能降级。
#
# 真实地址/密钥不入仓：以下默认值为占位，部署时通过环境变量注入真实值，例如：
#   REMOTE_HOST=<真实数据服务器IP> IDENTITY_FILE=/root/.ssh/<真实IP>_ssh.key \
#     sh deployment/docker/setup-sshfs-mounts.sh
set -eu

REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_HOST="${REMOTE_HOST:-<DATA_SERVER_IP>}"
REMOTE_PORT="${REMOTE_PORT:-22}"
KBASE_REMOTE="${KBASE_REMOTE:-/srv/safetyraise-data/kbase}"
RUNTIME_REMOTE="${RUNTIME_REMOTE:-/srv/safetyraise-data/runtime}"
KBASE_LOCAL="${KBASE_LOCAL:-/srv/safetyraise/kbase}"
RUNTIME_LOCAL="${RUNTIME_LOCAL:-/srv/safetyraise/runtime}"
IDENTITY_FILE="${IDENTITY_FILE:-/root/.ssh/<DATA_SERVER_IP>_ssh.key}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-docker_backend_1}"

KBASE_UNIT="${KBASE_UNIT:-srv-safetyraise-kbase.mount}"
RUNTIME_UNIT="${RUNTIME_UNIT:-srv-safetyraise-runtime.mount}"
REFRESH_UNIT="${REFRESH_UNIT:-safetyraise-backend-bindrefresh.service}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 执行本脚本。" >&2
  exit 1
fi

if ! command -v sshfs >/dev/null 2>&1; then
  echo "未找到 sshfs，请先执行 deployment/docker/provision-212.sh。" >&2
  exit 1
fi

if [ "$REMOTE_HOST" = "<DATA_SERVER_IP>" ]; then
  echo "REMOTE_HOST 仍为占位符，请通过环境变量注入真实数据服务器地址后重试。" >&2
  exit 1
fi

if [ ! -f "$IDENTITY_FILE" ]; then
  echo "缺少 SSH 私钥：$IDENTITY_FILE" >&2
  exit 1
fi

mkdir -p "$KBASE_LOCAL" "$RUNTIME_LOCAL"

# systemd .mount 单元名必须与挂载点路径转义后一致；如自定义路径请相应调整 *_UNIT 变量。
cat > "/etc/systemd/system/${KBASE_UNIT}" <<UNIT
[Unit]
Description=SafetyRAISE kbase (sshfs from data server)
After=network-online.target
Wants=network-online.target

[Mount]
What=${REMOTE_USER}@${REMOTE_HOST}:${KBASE_REMOTE}
Where=${KBASE_LOCAL}
Type=fuse.sshfs
Options=_netdev,IdentityFile=${IDENTITY_FILE},StrictHostKeyChecking=accept-new,reconnect,ro,port=${REMOTE_PORT},ServerAliveInterval=15,ServerAliveCountMax=3
TimeoutSec=40

[Install]
WantedBy=multi-user.target
UNIT

cat > "/etc/systemd/system/${RUNTIME_UNIT}" <<UNIT
[Unit]
Description=SafetyRAISE runtime (sshfs from data server)
After=network-online.target
Wants=network-online.target

[Mount]
What=${REMOTE_USER}@${REMOTE_HOST}:${RUNTIME_REMOTE}
Where=${RUNTIME_LOCAL}
Type=fuse.sshfs
Options=_netdev,IdentityFile=${IDENTITY_FILE},StrictHostKeyChecking=accept-new,reconnect,port=${REMOTE_PORT},ServerAliveInterval=15,ServerAliveCountMax=3
TimeoutSec=40

[Install]
WantedBy=multi-user.target
UNIT

cat > "/etc/systemd/system/${REFRESH_UNIT}" <<UNIT
[Unit]
Description=Refresh backend container bind mounts after sshfs is up
After=network-online.target docker.service ${KBASE_UNIT} ${RUNTIME_UNIT}
Wants=network-online.target
Requires=${KBASE_UNIT} ${RUNTIME_UNIT} docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker restart ${BACKEND_CONTAINER}

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${KBASE_UNIT}" "${RUNTIME_UNIT}"
systemctl enable "${REFRESH_UNIT}"

echo "已固化 sshfs 挂载单元并启用开机自启："
echo "  - ${KBASE_UNIT}  -> ${KBASE_LOCAL} (ro)"
echo "  - ${RUNTIME_UNIT} -> ${RUNTIME_LOCAL} (rw)"
echo "  - ${REFRESH_UNIT}（开机在挂载就绪后刷新 ${BACKEND_CONTAINER} 的 bind mount）"
echo "如需立即刷新 backend：systemctl start ${REFRESH_UNIT}"
