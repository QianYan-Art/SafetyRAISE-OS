# 部署说明

## 当前推荐拓扑

当前项目已经不再按“应用、数据库、知识库、reranker 全部同机”的方式部署。
推荐的生产拓扑是两台服务器：

1. `212` 应用服务器
2. `213` 数据 / 知识库服务器

其中：

1. `212` 运行 `frontend + backend`
2. `213` 运行 PostgreSQL，并托管知识库目录、后续增量数据与索引产物
3. `reranker` 当前默认停用，不再作为 212 新服务器的部署前提

当前代码主链路仍然是 `hybrid_local`，所以第一阶段的双机部署方式是：

1. `213` 保存知识库目录
2. `213` 同时保存运行时大文件目录
3. `212` 通过远端挂载读取知识库目录，并把运行时目录挂到 backend 容器
4. `backend` 容器继续按本地文件系统路径访问 `/opt/ts-analysis/kbase` 与 `/app/backend/data`

这不是最终的“检索服务化”形态，但它能在不重写整条检索链的前提下，先满足双机协同部署。

## 两台机器各自放什么

### 212 应用服务器

建议放这些内容：

1. 项目仓库与 `.env.server`
2. Docker / Docker Compose
3. `frontend` 容器（Nginx + 静态资源）
4. `backend` 容器（API、工作流、导出）
5. `models` 目录
6. `letsencrypt` 证书目录
7. 从 213 远端挂过来的只读知识库目录
8. 从 213 远端挂过来的运行时目录

### 213 数据 / 知识库服务器

建议放这些内容：

1. PostgreSQL
2. 知识库目录与 dense / sparse 索引文件
3. 后续用于知识库增量更新、重建索引的脚本和数据
4. 运行时共享目录
5. 可选备份目录

## 当前必须放行的端口

按当前方案，至少需要：

1. `212/tcp/80`：HTTP 首次访问与证书校验
2. `212/tcp/443`：HTTPS 正式流量
3. `212/tcp/23333`：SSH 运维
4. `213/tcp/5432`：PostgreSQL，仅允许来自 `212`

如果 212 通过 SSHFS 挂载 213 的知识库目录：

1. 复用 `213/tcp/22`
2. 不需要额外新增 NFS / SMB 端口
3. 但安全组至少要保证 `212 -> 213:22` 可达

如果 212 还把运行时目录挂到 213：

1. 仍然复用 `213/tcp/22`
2. 不需要额外新增端口

## 为什么当前不部署 reranker

原因很直接：

1. `212` 资源紧张
2. 当前 `<MODEL_API_HOST>` 只有 embedding，没有 reranker
3. 现阶段优先保证主报告链路和双机部署稳定

因此当前默认采用：

1. `sparse + dense + RRF merge`
2. 不启本地 `retrieval-reranker` sidecar
3. 如果未来需要恢复 rerank，再单独引入远端服务或便宜的外部 API

## 环境文件用法

公开仓库提供：

```text
.env.example
```

部署时复制为：

```text
.env.server
```

例如：

```bash
cp .env.example .env.server
```

然后至少填这些内容：

1. 宿主机挂载目录
2. 域名与邮箱
3. 模型服务地址
4. API Key
5. 数据库 DSN
6. 鉴权密钥与管理员初始账号
7. 知识库远端挂载目录
8. 运行时远端挂载目录
9. YOLO 权重目录

第一次部署前，建议先对照：

```text
docs/prepare-runtime-assets.md
```

把下面几件事准备好：

1. YOLO 权重文件
2. embedding 服务
3. PostgreSQL
4. 知识库目录
5. 域名 DNS 已指向 212

## Docker Compose 入口

主要文件：

```text
deployment/docker/docker-compose.server.yml
```

辅助脚本：

```text
deployment/docker/server-compose.sh
deployment/docker/provision-212.sh
deployment/docker/mount-213-kbase.sh
```

推荐通过脚本调用：

```bash
sh deployment/docker/server-compose.sh up -d --build
```

这个脚本会固定读取仓库根目录的 `.env.server`。

## backend 配置切换

Compose 会给 backend 容器设置：

```text
WORKFLOW_CONFIG_PATH=/app/backend/config/workflow.server.yaml
```

所以容器内默认走服务器版配置，而不是本地开发版配置。

## 212 上的推荐部署顺序

建议按下面顺序做：

1. 在 `212` 执行：

```bash
sh deployment/docker/provision-212.sh
```

说明：

1. `Debian 12` 默认软件源不一定提供 `docker compose plugin`
2. 当前脚本已经兼容两种形式：
   - 有 plugin 时用 `docker compose`
   - 没 plugin 时回退到 `docker-compose`
3. 当前脚本还会预写 `/etc/docker/daemon.json`，把宿主机默认 Docker 日志限制为 `json-file + 20m * 5`

2. 把仓库放到 `212`，准备 `.env.server`
3. 在 `213` 准备 PostgreSQL、知识库目录与运行时目录
4. 把 `213` 的 SSH key 放到 `212`，然后执行：

```bash
IDENTITY_FILE=/root/.ssh/<DATA_SERVER_IP>_ssh.key sh deployment/docker/mount-213-kbase.sh
```

```bash
IDENTITY_FILE=/root/.ssh/<DATA_SERVER_IP>_ssh.key sh deployment/docker/mount-213-runtime.sh
```

> 上面两条是一次性手动挂载（重启后会丢失）。生产环境请改用持久化方案，把挂载固化为 systemd 单元并启用开机自启与重启自愈：
>
> ```bash
> REMOTE_HOST=<DATA_SERVER_IP> IDENTITY_FILE=/root/.ssh/<DATA_SERVER_IP>_ssh.key \
>   sh deployment/docker/setup-sshfs-mounts.sh
> ```
>
> 该脚本生成 `srv-safetyraise-kbase.mount` / `srv-safetyraise-runtime.mount`（开机自启）与 `safetyraise-backend-bindrefresh.service`（开机在挂载就绪后刷新 backend 的 bind mount）。应用服务器重启后挂载会自动恢复，无需人工介入。

5. 让 `212` 能访问 `213:5432`
6. 执行：

```bash
sh deployment/docker/server-compose.sh up -d --build
```

说明：

1. `212` 当前是 `20G` 根盘，视频依赖必须走 CPU 版 `torch/torchvision`
2. 如果直接拉 Linux 默认 CUDA 包，构建阶段会把磁盘打满，backend 无法起容器
3. 当前 `deployment/docker/backend.Dockerfile` 已按 CPU 版安装策略收口

7. 再执行：

```bash
sh deployment/docker/setup-https.sh
```

8. 最后做健康检查、登录验证、报告链路验证

说明：

1. `setup-https.sh` / `renew-https.sh` 现在按 compose service label 查找 frontend 容器，不再依赖旧的硬编码容器名
2. HTTPS 脚本不会再 `source` 整份 `.env.server`；它们只按 key 读取 `LETSENCRYPT_*` / `FRONTEND_*` 字段，避免被中文显示名等业务配置污染
3. 在证书真正申请成功前，至少要先让 `/srv/safetyraise/nginx/default.conf` 生效
4. 如果直接用 `127.0.0.1` 或公网 IP 访问，没有带正式域名 `Host`，默认站点会返回 `444`
5. 当前 `frontend / backend` 的 Docker 日志已经在 compose 里显式限制为 `json-file + 20m * 5`
6. `setup-https.sh` 还会同步写入 `/etc/logrotate.d/safetyraise-cert-renew`，避免续期日志无限增长

## Nginx / HTTPS 相关脚本

### `server-compose.sh`

作用：

1. 包装 `docker compose`
2. 固定 `.env.server`
3. 固定 `docker-compose.server.yml`
4. 自动兼容 `docker compose` 与 `docker-compose`

### `setup-https.sh`

作用：

1. 先写入 HTTP 校验版 Nginx 配置
2. 拉起 frontend 容器
3. 调用 certbot 申请证书
4. 证书成功后切换到 HTTPS 配置
5. 写入自动续期 cron
6. frontend 容器定位按 compose service label，不再依赖旧容器名
7. 不再 `source .env.server`，避免 `BOOTSTRAP_ADMIN_DISPLAY_NAME=SafetyRAISE 管理员` 这类配置把 shell 脚本打断
8. 同步写入 `/etc/logrotate.d/safetyraise-cert-renew`，把续期日志限制为 `daily + rotate 14`

### `renew-https.sh`

作用：

1. 用 certbot 做续期
2. 成功后 reload frontend 容器内的 Nginx
3. frontend 容器定位按 compose service label，不再依赖旧容器名
4. 不再 `source .env.server`，只读取续期实际需要的少数字段

## 模型资产建议

如果你希望服务器部署尽量接近当前默认配置，建议这样准备：

1. YOLO：下载 `yolo11n.pt`，挂到 `MODELS_HOST_PATH`
2. embedding：当前生产配置默认走 `https://<MODEL_API_HOST>/v1`
3. 专家模型：当前生产配置默认走 `https://<MODEL_API_HOST>/v1`，模型名用实际服务里的 `safetyraise`
4. 报告 / 视觉模型：优先用远端 API，减少显存和部署复杂度
5. reranker：当前不作为必需资产
6. 会话与运行时状态：当前已改为 PostgreSQL 持久化
7. 旧报告输出目录：服务端默认只额外保留最近 `60` 个“未被当前会话结果引用”的旧输出目录，避免 213 上无限堆积
8. Dense 索引：当前可用 `backend/app/tools/build_dense_index.py` 基于现有 embedding 端点重建，再同步到 `213:/srv/safetyraise-data/kbase/data`

## 宿主机需要准备什么

至少准备：

### 212

1. Docker / Docker Compose
2. `.env.server`
3. 远端挂载后的知识库目录
4. 远端挂载后的运行时目录
5. YOLO 权重目录
6. 若启用视频链路，不要求 GPU，但要确认镜像按 CPU 版 `torch/torchvision` 构建
7. 若模型跑在宿主机，还要保证容器能访问到对应端口

### 213

1. PostgreSQL
2. 知识库目录
3. 备份目录

## 最容易踩的坑

1. `.env.server` 填了变量，但 backend 实际读的是另一个配置文件
   - 先确认 `WORKFLOW_CONFIG_PATH`。
2. `docker-compose` 老版本不支持 BuildKit 专属写法
   - 当前 Dockerfile 已去掉 `RUN --mount=type=cache`，不要再改回去。
3. shell / entrypoint / Dockerfile 被 Windows 换行污染成 `CRLF`
   - 212 上会表现成 `Illegal option -` 或 `no such file or directory`。
4. frontend 容器已经起来，但 `/srv/safetyraise/nginx/default.conf` 还是空的
   - 这时访问 `80/443` 看起来像服务不通，实质是没有生效的 Nginx 站点配置。
5. `ready=false` 不一定是后端挂了
   - 当前 212 的典型降级项是：知识库目录为空、YOLO 权重缺失、embedding 探测超时。
6. 会话与运行时目录曾留在 212 本地
   - 当前已经分别改为 PostgreSQL 持久化和 213 远端挂载。
7. HTTPS 脚本直接 `source .env.server`
   - 当前脚本已经修正为按 key 读取，后续不要再改回去，否则带空格/中文的环境值会让证书脚本直接失败。
8. 报告输出目录无限增长
   - 当前 server 配置已启用 `app.output_retain_count: 60`，只清理未被当前会话引用的旧输出目录；如果要保留更多历史，再按 213 磁盘容量调大。
9. 试图在 212 上直接回写 `/srv/safetyraise/kbase/data`
   - 当前 `kbase` 在应用机侧按只读挂载使用；真正写入 dense 索引时，要在 `213:/srv/safetyraise-data/kbase/data` 本机落盘。
2. `DATABASE_DSN` 仍然指向 `127.0.0.1`
   - 双机部署时必须改成 `213` 的真实地址。
3. 知识库目录挂载成功，但内部文件名不匹配
   - 重点核对 `manifest / chunks / rules / dense_*`。
4. 212 没有真正挂上 213 的知识库目录，但 backend 容器仍然启动成功
   - 重点检查 `/api/v1/ready`，不要只看 `/health`。
5. 模型服务地址写了根地址，但真实接口不兼容 OpenAI Chat Completions
   - 先用 `curl` 打通再接到系统里。
6. 证书脚本跑通了，但 Nginx 配置中的域名仍是示例值
   - 记得修改 `.env.server`，必要时同时改示例配置模板。
7. 只拉起前端容器，忘了 backend
   - 先用 `docker ps` 看两类容器是否都在。

## 部署后第一组检查

建议按以下顺序检查：

1. `docker ps`
2. `sh deployment/docker/server-compose.sh logs backend`
3. `curl /api/v1/health`
4. `curl /api/v1/ready`
5. 登录页能否打开
6. 管理员 `safetyraise` 能否登录并进入管理控制台
7. 普通用户能否注册，并弹出个人模型配置抽屉
8. 图片链路能否跑通
9. 再测视频、导出和历史会话
