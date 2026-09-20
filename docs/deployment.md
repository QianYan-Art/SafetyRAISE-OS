# 部署说明

## 小容量主机资源门

Compose 默认后端 `BACKEND_MEMORY_LIMIT=1536m`、`BACKEND_CPU_LIMIT=2`、
`BACKEND_PIDS_LIMIT=256`，数学库线程通过 `BACKEND_MATH_THREADS=2` 控制；
前端默认 `FRONTEND_MEMORY_LIMIT=128m`、`FRONTEND_CPU_LIMIT=0.5`、
`FRONTEND_PIDS_LIMIT=64`。它们是待负载验证的部署起点，不是已证明的容量。
既有 `.env.server` 中的显式值优先，调整前核对实际生效配置，不输出密钥。

一次212隔离业务实测中，768MiB容器在加载知识索引后无法再保留256MiB余量，
触发保护且没有OOM；仅将该容器改为1GiB并禁止额外swap后可恢复运行。
这不是完整视频流程的容量认证。Docker stats扣除部分缓存后的数值不等于
`memory.current`；配置须同时检查cgroup峰值、宿主余量和实际视频负载。

`DOCKER_LOG_MAX_SIZE=10m` 与 `DOCKER_LOG_MAX_FILE=3` 限定单容器日志轮转；
后端增加仅访问本地 `/api/v1/health` 的存活探针，探针不调用模型。
存活200不代表模型、完整链路或报告质量已验收。

完整报告运行的资源检查必须覆盖应用盘、实际运行数据挂载和可用内存；
Linux 内存检查同时考虑宿主 `MemAvailable` 与 cgroup v2 剩余额度。
默认仅允许一个活动完整报告运行，跨进程通过数据库事务锁核验。
磁盘/内存不足时停止新增外发并保留可恢复状态，不通过自动删除业务资料腾空间。

正式切换前保留短期回滚副本，健康与功能验证完成后才按明确清单清理旧部署、
旧镜像和备份；不使用全局 Docker prune。213 上仍被挂载使用的知识库、
数据库和运行数据不是旧版本备份，不在应用版本清理范围内。

## 完整业务开发入口

主应用另有默认关闭的正式装配入口：服务端同时设置
`report_harness.enabled=true`、`online_enabled=true`，提供绝对路径
`runtime_manifest_path` 与 `resource_paths`。启动时验证数据库 schema、既有四角色合同、
知识摘要、实际代码清单和只读批准绑定；不是运行开发服务器后自动切换正式模式。

正式Docker部署须显式叠加 `deployment/docker/docker-compose.harness.yml`。
它不会被基础脚本自动加载，不创建批准或预算；外部配置的命令行 `--config`
与 `WORKFLOW_CONFIG_PATH` 同时指向 `/run/safetyraise/harness/workflow.server.yaml`。

批准目录及其父目录必须对应用身份不可写。构建产物须包含代码清单核对的后端源码、
前端源码与依赖清单；不能用缺文件的镜像绕过校验。费用 SQLite 必须放在运行主机本地盘，
不要放入 SSHFS 共享目录；迁移原账本需保留所有已知、未知和预留记录，不新建空账本重置额度。
资源默认阈值为2048MiB磁盘、256MiB内存，执行期间持续复核；查阅和取消不因空间不足被拒绝。
启动时仅资源不足或资源探针失败允许降级到查阅和取消，生成入口保持关闭。
释放资源后重启服务重新装配；不会自动恢复或重试付费请求。
代码、批准、知识或合同错误仍拒绝启动，不回退旧生成链路。
此处描述源码要求，不宣称服务器已启用、已迁移或真实模型质量已通过。

在目标运行主机的 `backend` 目录使用项目 Python 执行
`python -m app.report_harness.business_server`。必需参数：

- `--manifest`：操作者批准的外部运行配置 JSON，不放入源码仓；包含知识内容摘要、
  已登记货币合同摘要、既有账本绝对路径及 experiment ID、两个报告角色的原配置端点名、
  四角色模型元数据、可选预算和明确确认的未知 attempt 编号。
- `--resource-path`：需要检查的实际应用及运行数据目录，可重复；账本所在盘自动加入。
- `--confirm-development-server`：明确启动该开发入口；不替代用户逐运行确认外发。
- `--port`：默认18282，仅监听目标主机的127.0.0.1，不自动发布公网入口。
- `--minimum-free-mib`：默认2048，逐挂载检查可用空间，不靠删除业务数据腾空间。
- `--minimum-memory-mib`：默认256；Linux检查宿主与cgroup，Windows检查可用物理内存。

启动器不下载知识库、不建新费用实验、不自动登记收费合同、不修改生产配置，
也不自动确认旧未知费用。合同中的角色、端点、模型、容量证明或预留报价不一致时拒绝启动。
原报告端点的非空 `extra_body` 尚未纳入此入口的费用证明，明确拒绝，不静默忽略其路由配置。
当前远端收费角色只装配 OpenRouter，供应商价格过滤使用每百万 token 的美元单价，
与本地预留换算共用已登记报价；自动回退关闭，不设置输出 token 上限。
原自有嵌入服务可继续使用，但必须已有明确 `local_token_free` 角色合同、可核验的
LM Studio 嵌入模型容量元数据，且与已配置专家使用同一基础端点；不能把任意收费端点
标成免费跳过记账。零货币费用仍记录物理请求和 token 消耗。
其他收费网关必须先实现相同的价格执行与核验合同，不能仅替换 URL 后启用。
未提供 `budget` 时，token 预留由四角色容量和默认物理请求次数计算；显式提供时
容量不足会启动失败，不再沿用小模型的8192预留值去限制大上下文模型。
这些是内部记账边界，不发给模型，也不调整既有货币账本额度。
专家容量预留使用标称 `max_context_length`，不证明实时加载容量相同，也不表示已按
tokenizer 校验输入长度。部署时需另外核实模型实际加载状态和输入容量。
专家、嵌入及报告端点原有的 `timeout_seconds` 被保留，并与整轮剩余时间共同约束请求。
未配置整轮活动时长时默认600秒；角色超时即使为1800秒，也不会自动扩大整轮预算。
部署长推理角色前须显式设置并核对完整 `budget`，包括四角色容量所需的 token 预留。
外层预算提前结束不能证明模型自身慢的根因；未知结果仍按原记录确认后才能重试。
合同迁移应逐角色读取最新总摘要；修订已有角色还须明确传入旧角色摘要。
只追加版本，不改旧账本金额与未知请求状态；迁移完成后再记录最终合同摘要到 manifest。
这条入口强制工程导出；正式质量门、部署切换及旧版清理必须另行完成验收。

2026-09-18只读核验：212生产后端挂载的 `/srv/safetyraise/runtime` 是来自213的SSHFS；
`runtime`、`models`、`kbase`、`nginx`、`letsencrypt` 是现有业务或共享资产，
全部排除在应用旧版本清理之外。执行清理前仍须重新核验实时挂载和引用，不凭此记录删除。

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

### 正式harness覆盖

完成真实业务核验和批准材料准备后，使用同一个Compose项目显式叠加覆盖文件，
不要另建长期并行生产服务。212当前只有 `docker-compose 1.29.2`，先核验以下外部路径，
再用两个 `-f` 参数执行 `docker-compose --env-file .env.server ... config --quiet`；
不要公开完整配置输出，也不要使用 Compose v2 的 `additional_contexts`。

- `HARNESS_WORKFLOW_CONFIG_HOST_PATH`：显式启用harness的完整服务端配置。
- `HARNESS_MANIFEST_HOST_PATH`：只读挂载为 `/run/safetyraise/harness/runtime-manifest.json`。
- `HARNESS_RELEASE_DIR_HOST_PATH`：包含实际角色模板、构建清单、批准表和验收证据的只读目录。
- `HARNESS_LOCAL_LEDGER_HOST_DIR`：212本地持久盘，容器路径 `/var/lib/safetyraise/ledger`，
  不是来自213的SSHFS目录。必须迁移原账本，不能用空文件重新开始费用计数。
- `HARNESS_LOCAL_TMP_HOST_DIR`：212本地受控临时盘，容器路径
  `/var/lib/safetyraise/harness-tmp`，必须与费用账本目录分开，也不能指向213的
  `runtime`、`kbase`或其他共享业务目录。宿主目录须预先给运行身份可写权限。
- `BACKEND_RUNTIME_BASE_IMAGE`：212本机已核验的完整 backend 运行镜像ID，覆盖层会将其
  映射为 `RUNTIME_BASE_IMAGE`，并使用 `safetyraise-backend:harness-runtime`；缺失时必须失败，
  不得回退到可变标签或联网拉取。
- `FRONTEND_RUNTIME_CONTEXT_HOST_PATH`：212本地预构建前端运行时 context，目录内只能有
  `Dockerfile`、`nginx.frontend.conf` 和 `dist`。发布准备步骤应从仓内
  `frontend.runtime.Dockerfile`、默认配置和已核验的 `frontend/dist` 复制生成该目录；
  不要把仓库根目录、213共享目录或带有其他源码/凭证的目录作为 context。

覆盖默认身份为 `HARNESS_UID=10001/HARNESS_GID=10001`，根文件系统只读、移除全部
capability、禁止提权，`TMPDIR=/var/lib/safetyraise/harness-tmp`；`/tmp`仍保留
128MiB小型tmpfs，仅供小型进程临时文件使用。这样不缩小既有1200m上传上限，
Starlette `UploadFile`的大文件临时内容落到212本地受控目录，而不是占满`/tmp`。
宿主写目录须预先配置权限。若旧SSHFS目录只有root可写，可显式选择UID/GID 0，
但必须保留全部只读隔离约束，实测应用对批准表及其父目录 `os.access(W_OK)` 为false。
不能递归更改共享资产权限，也不能把可写Git检出目录当成批准源。

基础 `docker-compose.server.yml` 仍使用 `backend.Dockerfile`，默认值刻意保留
`INSTALL_VIDEO_DEPS=false`；正式 harness 覆盖改用 `backend.runtime.Dockerfile`，并强制
`INSTALL_VIDEO_DEPS=true`、将必填 `BACKEND_RUNTIME_BASE_IMAGE` 映射为
`RUNTIME_BASE_IMAGE`，输出镜像标签为 `safetyraise-backend:harness-runtime`。
不能用缺少视频依赖的新镜像替换已有完整服务。操作前固定并记录基础镜像完整ID，构建时
禁止拉取或联网，避免可变标签悄悄换源；构建后再次记录实际基础与成品ID。
Windows准备Linux构建context时，必须用
`git -c core.autocrlf=false -c core.eol=lf archive` 固定归档换行，
并逐文件核对归档内容与构建清单。普通 `git archive` 可能受Windows换行配置影响，
不能仅凭commit相同就认定归档字节与清单一致；不用修改全局Git配置。
该路径清除新镜像内旧应用源码后复制本次源码，使用
`verify-runtime-dependencies.py` 核验版本、视频模块及配置中的ffmpeg/ffprobe实际执行，
再运行 `pip check`，不自动安装依赖。
native库通过 `COPY --from` 从核实的基础阶段继承，仅可在原生源码已核实一致时复用；
依赖或原生源码变化须回到完整构建。
这两种构建都不能替代镜像内源码清单、批准权限、真实视频链路和上线资源验收。

前端正式覆盖使用 `deployment/docker/frontend.runtime.Dockerfile`，通过必填
`FRONTEND_RUNTIME_BASE_IMAGE` 复用212本机已核验的完整Nginx镜像ID；当前已核实的值为
`sha256:a063c54671daf163bcb8e41fab2dc657546ac98c0b32e4769ca245dbf1d1c344`，部署前仍须
确认该镜像在本机存在。旧版 `docker-compose 1.29.2` 不支持 `additional_contexts`，因此
构建时把仓内 Dockerfile 复制为 context 根的 `Dockerfile`，并从同一 context 直接复制
`dist` 和 `nginx.frontend.conf`。该路径不执行 `npm ci`、不拉取Node依赖。构建时必须
显式禁止拉取（使用 `--pull=false` 或等价的离线构建选项），并记录完整基础镜像ID与成品ID。
隔离配置下前端离线构建和 `nginx -t` 已通过，129MiB Starlette 临时上传也已在
64MiB `/tmp` 下转入本地临时盘并在请求关闭后清空。2026-09-20另用临时容器只读挂载
现有正式站点配置、证书和ACME目录，`nginx -t`通过；未启动监听端口或切换正式服务，
实际公网请求、代理上传和业务链路仍须在发布时验证。
该Dockerfile只写入镜像层；正式运行仍可按现有Compose继承挂载已核验的站点配置、证书
和ACME目录，全部保持只读，不修改宿主配置或证书。

212根盘当前 `df` 实测约12GB可用；约1.44GiB是可用内存，不是磁盘余量。仍必须持续保护
空闲空间。清理临时文件前先确认没有活动上传或正在
使用该目录的请求；只允许清理 `HARNESS_LOCAL_TMP_HOST_DIR` 对应目录的内容，不能泛删
`/tmp`、费用账本、213共享runtime/kbase或业务媒体/输出目录。清理策略不得作为后台服务
自动运行，必须在有界、可核验的维护窗口执行。

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
