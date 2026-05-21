# 部署说明

## 总体思路

仓库内置的是一套 Docker Compose 部署骨架，目标是把前端、后端和 reranker sidecar 一起拉起：

1. `frontend`
2. `backend`
3. `retrieval-reranker`

其中：

1. `frontend` 负责静态资源与反向代理
2. `backend` 负责 API、工作流、导出
3. `retrieval-reranker` 负责对召回片段做重排

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
5. 知识库目录
6. YOLO 权重目录

第一次部署前，建议先对照：

```text
docs/prepare-runtime-assets.md
```

把下面四件事准备好：

1. YOLO 权重文件
2. embedding 服务
3. reranker 服务
4. 最小知识库文件

## Docker Compose 入口

主要文件：

```text
deployment/docker/docker-compose.server.yml
```

辅助脚本：

```text
deployment/docker/server-compose.sh
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

## Nginx / HTTPS 相关脚本

### `server-compose.sh`

作用：

1. 包装 `docker compose`
2. 固定 `.env.server`
3. 固定 `docker-compose.server.yml`

### `setup-https.sh`

作用：

1. 先写入 HTTP 校验版 Nginx 配置
2. 拉起 frontend 容器
3. 调用 certbot 申请证书
4. 证书成功后切换到 HTTPS 配置
5. 写入自动续期 cron

### `renew-https.sh`

作用：

1. 用 certbot 做续期
2. 成功后 reload frontend 容器内的 Nginx

## reranker sidecar 做什么

当前默认 sidecar 是 Hugging Face 的 text-embeddings-inference 镜像，用来承载：

```text
Alibaba-NLP/gte-multilingual-reranker-base
```

它的职责很单纯：

1. 接收候选知识片段
2. 输出重排分数
3. 帮助 hybrid 检索把最相关片段排到前面

如果你只保留稀疏或 dense 召回，不做 rerank，也可以自行裁剪这部分。

## 模型资产建议

如果你希望服务器部署尽量接近仓库默认配置，建议这样准备：

1. YOLO：下载 `yolo11n.pt`，挂到 `MODELS_HOST_PATH`
2. embedding：单独准备一个本地 OpenAI 兼容 embeddings 服务
3. reranker：直接使用本仓库 Compose 里的 `retrieval-reranker`
4. 专家模型：用宿主机本地服务承载，再通过 `host.docker.internal` 暴露给 backend
5. 报告 / 视觉模型：优先用远端 API，减少显存和部署复杂度

## 宿主机需要准备什么

至少准备：

1. Docker / Docker Compose
2. `.env.server`
3. 知识库目录
4. YOLO 权重目录
5. 若启用视频链路，最好有 GPU 环境
6. 若模型跑在宿主机，还要保证容器能访问到对应端口

## 最容易踩的坑

1. `.env.server` 填了变量，但 backend 实际读的是另一个配置文件
   - 先确认 `WORKFLOW_CONFIG_PATH`。
2. 知识库目录挂载成功，但内部文件名不匹配
   - 重点核对 `manifest / chunks / rules / dense_*`。
3. 模型服务地址写了根地址，但真实接口不兼容 OpenAI Chat Completions
   - 先用 `curl` 打通再接到系统里。
4. 证书脚本跑通了，但 Nginx 配置中的域名仍是示例值
   - 记得修改 `.env.server`，必要时同时改示例配置模板。
5. 只拉起前端容器，忘了 backend 或 reranker
   - 先用 `docker ps` 看三类容器是否都在。

## 部署后第一组检查

建议按以下顺序检查：

1. `docker ps`
2. `sh deployment/docker/server-compose.sh logs backend`
3. `curl /api/v1/health`
4. `curl /api/v1/ready`
5. 前端首页能否打开
6. 图片链路能否跑通
7. 再测视频、导出和历史会话
