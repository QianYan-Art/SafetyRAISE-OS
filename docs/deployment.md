# 部署说明

本文描述部署契约和验证步骤，不记录某次迁移的状态。公开仓库不包含生产凭据、事故材料、知识库正文、费用账本或已批准的 Harness 发布包。部署前先阅读[配置说明](configuration.md)、[运行资产准备](prepare-runtime-assets.md)和[报告 Harness](report-harness.md)。

## 拓扑与入口

同机部署时，宿主 Nginx 终止 TLS，前端容器只绑定回环端口并代理 `/api/` 到后端；后端通过 Docker 内部网络访问 PostgreSQL，知识库只读挂载，费用账本和临时上传落在应用机本地持久盘。建议目录分层：

| 类型 | 路径 | 要求 |
| --- | --- | --- |
| 应用、发布与私有编排 | `/srv/apps/safetyraise` | 私有目录 `0700`，含凭据文件 `0600` |
| 数据库与知识库 | `/srv/data/safetyraise` | 不随应用发布删除；知识库挂载只读 |
| 账本、模型权重、上传临时盘 | `/srv/data/safetyraise-app` | 账本使用本地磁盘，不使用 SSHFS |
| Nginx 日志 | `/srv/logs/nginx` | 配置轮转与容量限制 |

仓库提供两套模板：

| 拓扑 | 适用 | 入口 |
| --- | --- | --- |
| 同机部署（推荐） | 应用、PostgreSQL 与知识库在同一台主机 | `prepare-single-host-release.py` 生成编排，宿主 Nginx 参考 `nginx.host-site.conf` |
| 分机部署 | 应用主机与数据主机分离，知识库和运行目录经 SSHFS 挂载 | `docker-compose.server.yml`、`server-compose.sh`、`provision-app-host.sh`、`setup-sshfs-mounts.sh` / `mount-remote-*.sh` |

两者不能混用。`prepare-single-host-release.py` 需要已经检查过的源编排、不可变镜像 ID、发布包和私有目录，它不是从空仓库自动生成生产环境的安装器。

证书续期脚本（同机的 `renew-host-nginx.sh`、分机的 `renew-https.sh`）必须安装在发布目录之外的固定位置，由 Certbot 钩子或 cron 调用；指向某个发布目录的续期任务会在清理旧发布后静默失效。

## 先决条件

1. PostgreSQL、强鉴权密钥、管理员初始凭据和数据库迁移；数据库端口不应向公网开放。
2. 可用的视觉、报告、Embedding 和系统级专家模型端点及对应服务端凭据。专家模型不在普通用户或管理员模型配置界面暴露；用户模型解析见[配置说明](configuration.md)。
3. 非空的真实知识库：manifest、chunks、rules、可选增强规则、search index 和与当前 Embedding 模型匹配的 dense 三件套。`examples/kbase/minimal/` 是零知识正文的结构模板，不能用作生产数据。增强规则存在且启用时，发布批准摘要必须包含实际读取的增强规则，而非基础规则。
4. YOLO 权重、`ffmpeg`、`ffprobe` 和包含 CPU 版 `torch/torchvision`、`ultralytics`、`lap` 的后端镜像。替换镜像前运行 `deployment/docker/verify-runtime-dependencies.py`。
5. Harness 在线运行还需要已有费用 SQLite 账本、四角色合同、外部 runtime manifest、实际构建清单、只读批准绑定和评估证据；代码或知识版本变化后重建并审核绑定。`demo` 模式输出带标记工程稿，不等同于正式发布。

所有密钥只写入受限的服务器配置，不放在 Compose 命令输出、仓库、镜像、前端或记录文档。`.env.example` 是配置字段示例；复制为 `.env.server` 后逐项替换占位与空值。生产配置下 `AUTH_JWT_SECRET` 与 `BOOTSTRAP_ADMIN_PASSWORD` 为空或不安全时后端拒绝启动，见[配置说明](configuration.md)。

## 发布

在仓库中运行相关后端测试、前端测试与构建；核对镜像内源码、依赖和已批准清单的一致性。迁移已有部署时，先保存可恢复的 PostgreSQL 逻辑备份与 SQLite backup API 一致性副本，并验证可读性。切换前停止旧后端写入，迁移最终账本；不可让两台后端同时写各自账本。

从经检查的私有源编排生成同机编排示例（路径和镜像 ID 按目标主机实际值填写）：

```sh
python3 deployment/docker/prepare-single-host-release.py \
  --source /srv/apps/safetyraise/private/compose.source.json \
  --output /srv/apps/safetyraise/private/compose.prod.json \
  --release-dir /srv/apps/safetyraise/releases/<release> \
  --app-root /srv/apps/safetyraise \
  --data-root /srv/data/safetyraise \
  --local-data-root /srv/data/safetyraise-app \
  --backend-image sha256:<verified-backend-id> \
  --frontend-image sha256:<verified-frontend-id> \
  --frontend-port 18080
docker compose -f /srv/apps/safetyraise/private/compose.prod.json config --quiet
docker compose -f /srv/apps/safetyraise/private/compose.prod.json -p safetyraise up -d
```

隔离验证使用 `--test-database`、独立输出、运行时、上传和账本目录，以及不同的前端回环端口；不能把测试库直接切为正式库。宿主 Nginx 参考 `deployment/docker/nginx.host-site.conf`（把 `example.com` 换成实际域名），证书续期钩子参考 `renew-host-nginx.sh`，安装到 Certbot 的 `renewal-hooks/deploy/` 目录而不是发布目录；配置前核对其他虚拟主机、证书和日志目录，执行 `nginx -t` 后才重载。Certbot 续期需做 `--dry-run`，并测试钩子重载。站点登记、日志轮转和每日备份须与宿主运维规范对齐。

## 验证与回滚

1. 检查 Compose 实际挂载：真实知识库为 `:ro`，运行时与账本路径正确，前端只监听回环；检查 backend/frontend/PostgreSQL 运行和重启计数。
2. 从宿主指定域名/SNI 检查 TLS、首页、`/api/v1/health` 和 `/api/v1/ready`；再以真实 HTTP 验证登录、会话隔离、事故信息保存和导出。`/ready` 只证明配置与依赖就绪，不证明法律引用或模型质量。
3. 受控报告验证需分别记录视觉、专家、检索、生成、审查和导出结果；付费请求与真实事故证据必须单独获得授权。前端合成路径可用 `frontend/tests/production-workspace-smoke.mjs`，它不调用模型、不能替代完整报告验收。
4. 观察资源门：宿主可用空间、`memory.current`/容器限额、OOM、临时上传、Nginx/容器日志、其他站点和备份 timer。`BACKEND_MEMORY_LIMIT=1536m` 只是公开 Compose 模板起点，不能覆盖已按负载实测调整的私有配置；发布前核对源、展开编排和实际容器限额一致。
5. 回滚前先停止当前后端写入，核对账本增量和数据库快照，再恢复旧镜像、编排和入口；不能只切 DNS 或直接启动旧后端。发布包和旧镜像保留到观察期结束且回滚路径完成验证。

费用账本的 WAL/SHM 不应直接做在线文件归档；用 SQLite backup API 生成一致性副本。PostgreSQL 用 `pg_dump` 逻辑备份，并用 `pg_restore --list` 检查可读性。备份策略要覆盖应用私有配置、数据库、账本、证书及知识库，不把自动轮转当成独立恢复演练。

## 专家模型（Modal）

专家模型可以用 `deployment/modal/qwen3_expert.py` 部署到你自己的 Modal 账户。脚本要求已有持久卷 `safetyraise-qwen3-f16`，并从卷内 `/models/TS-Qwen3` 读取完整模型；卷不存在时直接失败，避免误建空卷后发布不可用端点。

```sh
modal volume create safetyraise-qwen3-f16
modal volume put safetyraise-qwen3-f16 <LOCAL_MODEL_DIR> /models/TS-Qwen3
modal deploy deployment/modal/qwen3_expert.py
```

脚本固定 vLLM `0.21.0`、L4、F16、单容器单并发、60 秒空闲缩容和 1800 秒启动等待，服务名为 `suyuan37/SafetyRAISE-TS-Qwen3`，上下文 `12288`，不设置输出 token 上限。部署完成后，把 Modal 给出的地址（加 `/v1`）写入 `EXPERT_LOCAL_BASE_URL`；在 Modal 控制台创建 Proxy Auth token，只保存到服务器的受限环境文件（如 `.env.server` 的 `MODAL_EXPERT_PROXY_TOKEN`），不写入仓库、镜像、前端或日志。

## 清理边界

只按明确清单删除已确认无运行、定时任务、发布或回滚引用的临时构建物。删除前后检查磁盘余量、数据库、`/health`、`/ready`、TLS 和其他站点；不运行全局 Docker prune，不删除业务媒体、知识库、数据库、账本、审计记录、证书或仍需的回滚资产。持续运行中的 DNS 缓存旧入口应在观察期和实际流量核验后再停用。
