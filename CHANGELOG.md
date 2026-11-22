# 变更记录

本文件记录面向使用者和部署者的重要变化，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。`0.x` 期间次版本号变化可能包含不兼容改动。

## [未发布]

## [0.3.14] - 2026-11-22

### 修复

- 修复 `secret_box.py` 的 PostgreSQL `pgcrypto` 加密/解密 SQL 表达式在 `psycopg` 服务端参数类型推导下触发 `IndeterminateDatatype` 的问题，为全部参数补充显式 `::text` 类型标注，并新增真实 PostgreSQL 集成回归测试。

## [0.3.13] - 2026-11-22

### 安全

- **SSRF 与系统密钥防外泄**：新增统一出站 URL 安全校验（`backend/app/core/outbound_url.py`），在用户模型配置保存与运行时出站前阻断回环、私网、链路本地、云元数据、非白名单 HTTP 及 `userinfo`/查询参数注入；覆写嵌入端点 URL 时不再沿用系统 `OPENROUTER_API_KEY`；停止将系统密钥明文播种进管理员 `user_capability_configs` 行，用户自定义 API Key 改用 PostgreSQL `pgcrypto` AES-256 加密落库（兼容历史明文读取）。
- **旧版模型客户端有界流式响应**：报告、视觉、专家、Embedding 与 Reranker 客户端统一启用 `trust_env=False`、`follow_redirects=False` 与 1 MiB 流式响应字节上限，防止超大/chunked 响应耗尽内存。
- **认证与权限加固**：
  - 生产 `AUTH_JWT_SECRET` 强制校验至少 32 字节且具备字符多样性（拒绝单字符与纯重复串），JWT 算法限制为 `HS256/HS384/HS512`，强制 `aud`/`iat`/`exp`/`sub` 校验。
  - 登录与注册新增 IP + 账号双维度滑动窗口限流（返回 `429 + Retry-After`）与有界 PBKDF2 并发槽位；登录对未知用户执行固定虚拟哈希校验以消除时序枚举差异；密码长度限制为 8–128 字符。
  - 新增 `users.token_version`、`POST /api/v1/auth/logout` 与 `POST /api/v1/auth/change-password`，登出、自助改密及管理员重置密码立即吊销旧访问令牌；媒体令牌改用域分离独立派生密钥并绑定 `session_id`。
  - 禁止普通注册/创建账号抢占引导管理员用户名；引导管理员初始化改用 `ON CONFLICT DO NOTHING` 消除并发启动竞态；管理员更新用户增加自降权/自停用、引导管理员保护及“至少保留一名活跃管理员”事务不变量，并补充认证/管理审计日志。
  - 新增 `auth.allow_self_registration` 与可选 `auth.registration_invite_code` 配置开关。
- **会话隔离与上传防护**：
  - 修复创建会话时传入他人 `id` 可能覆盖他人会话的 IDOR 风险；`ChatSessionService` 默认 fail-closed，跨用户管理操作须显式声明 `allow_admin_scope=True`。
  - 上传链路新增白名单扩展名与真实文件头魔数（magic bytes）校验，阻断伪造 MIME 的 `.html`/`.svg` 等主动内容上传；媒体下载路由对非安全图片/视频强制 `application/octet-stream` + `attachment` + `nosniff` + `Content-Security-Policy: default-src 'none'; sandbox`；`upload_manifest` 整数解析异常统一返回 400。
- **公开接口、请求体与响应头硬化**：
  - 公开 `GET /api/v1/ready` 默认仅执行本地低成本检查并短 TTL 缓存，不再对匿名请求触发付费嵌入或外部模型探测；主动外部探测仅限管理员显式 `probe=active`。
  - 新增全局分层 JSON 请求体上限中间件（`/api/v1/auth/*` 64 KiB、报告运行/证据 256 KiB、常规 JSON API 2 MiB）；默认收紧 CORS 白名单并为全站 API 响应与 Nginx 模板补齐安全响应头及 `/api/v1/auth/` 独立 64 KiB 限制。
  - 非管理员调用旧版报告接口不再覆盖全局共享 `backend/data/input_accident.json`，持久化写入改为原子写 + 微秒级防冲突备份；`.gitignore` 补充 `.env.*`（保留 `!.env.example`）。
  - 前端登出或令牌失效时同步吊销服务端令牌并清除 `localStorage` 中全部用户会话 PII 缓存；Markdown 渲染显式限定 `https:`/`mailto:` 及同源相对 URL 并附带 `rel="noopener noreferrer"`。
- **容器、运维脚本与 CI 门禁**：`backend.Dockerfile` 改以专用非 root 用户（UID/GID 10001）运行，`docker-compose.server.yml` 补齐 `read_only`、`cap_drop: ALL` 与 `no-new-privileges`；`setup-https.sh` 增加域名白名单校验并修复 ShellCheck 警告；CI 新增 Rust `cargo clippy -- -D warnings`、`cargo test` 与生产 `npm audit` 检查。

### 修复

- **JSON 解析器与 Rust FFI 一致性**：`json_parser.py` 捕获 `ast.literal_eval()` 的 `TypeError`/`RecursionError` 等异常并统一转为 `InputValidationError`（不再绕过视觉 JSON 修复重试），限制平衡扫描起点数与嵌套深度；Rust `query_token_accel` 去除候选提取阶段的双重修补以对齐 Python 语义、将 C ABI 原始指针函数标记为 `unsafe extern "C"`、清零全部 6 项 Clippy 严格告警并补充单元测试。
- **导出与输出硬化**：PDF 封面字段增加 XML/HTML 转义防止 ReportLab 标记异常；`sanitize_model_text` 剥离未闭合推理标签/围栏；`np.load` 显式传入 `allow_pickle=False`；SQLite 费用账本初始化在 POSIX 上收紧目录/文件权限为 `0700`/`0600`。

## [0.3.12] - 2026-10-31

### 安全

- 前端开发与测试依赖升级：`vite` 8.3.0→8.3.5、`vitest` 与 `@vitest/mocker` 5.0.1→5.0.5、`undici` 8.10.2→8.12.3（经 `jsdom`），连带更新 `rolldown` 等构建子依赖，关闭 13 条 Dependabot 告警；均为开发依赖，运行时依赖不变，`npm audit` 为 0。

## [0.3.11] - 2026-10-31

### 新增

- 报告页“检索片段”改为“参考依据”：列出初始检索、模型检索词与命中数、读取全文的完整记录，并按报告引用、读取全文、模型检索命中、初始检索列出全部知识条目，标明撰写或审查模型使用，原文可展开。`materials` 接口新增 `lookups` 与 `references`。

## [0.3.10] - 2026-10-31

### 修复

- 历史报告下拉中的状态与状态行一致：因审查或候选稿终检停止的报告显示“未通过独立审查”，当前报告直接沿用状态行文字。

## [0.3.9] - 2026-10-31

### 变更

- 去掉状态行的“生成过程”按钮，生成过程统一从页头处理记录进入；处理记录中的进度卡与其他记录同宽对齐。
- 窄屏下历史报告选择铺满一行，标签页间距收紧以完整显示，步骤与用时合并为一行说明，不再出现行首分隔点。

### 修复

- 报告详情、事件、中间产物与候选稿改为并行读取，首屏不再等事件读完才显示正文；候选稿只在版本或状态变化时重读；轮询遇到仍在进行的读取时跳过，不再作废刚取回的结果。

## [0.3.8] - 2026-10-31

### 变更

- 查看报告页重新划分职责：主区域只保留报告本身（标题与历史报告、单行状态与操作、下载提示，以及报告正文、专家指导意见、检索片段、补充材料四个标签页）；逐步生成过程只在当前档案处理记录顶部显示，与资料、草稿阶段使用同一种进度卡。
- 问题均已闭合、仅审查结构未完成时，状态改为“未通过独立审查”，不再显示“报告仍有待解决的问题”。

## [0.3.7] - 2026-10-31

### 新增

- 重新生成报告时关联本档案最近一份报告；事实快照与端点配置不变时沿用其专家指导意见，不再调用专家模型，生成过程标注复用。
- 报告区恢复“专家指导意见”与“知识库检索片段”预览，数据来自新接口 `GET /api/v1/report-runs/{id}/materials`。

## [0.3.6] - 2026-10-31

### 修复

- 刷新或重新打开已有 Harness 报告的档案时停在“核对事实”。此前工作台只按旧报告字段判断步骤，服务端报告运行记录不计入；现在读取到运行记录后进入“查看报告”。

## [0.3.5] - 2026-10-31

### 修复

- 审查模型漏填逐义务 coverage 清单时，先把缺项交还审查者补齐（与取证缺口共用最多两次协议修复），不再直接以“义务 coverage 检查不完整”终止运行；发布前终检规则不变。
- 生成用时超过一小时按“小时 分”显示；当前档案处理记录中的报告生成进度卡置顶。

## [0.3.4] - 2026-10-31

### 变更

- 报告执行改由服务端后台任务持有：刷新、断网或关闭页面只断开事件流，报告继续生成，重新打开档案即可看到进度；只有“停止生成”或删除会话会取消。此前事件流断开会直接取消运行。服务重启时仍在执行的运行会被取消并记录。

### 新增

- 报告区显示“生成过程”：专家分析、检索、撰写、审查和修订轮次及各步用时，运行中实时更新；当前档案处理记录同步显示报告生成进度。

## [0.3.3] - 2026-10-30

### 修复

- Modal 专家部署脚本不再限定 `region="eu"`，GPU 改为 L4 优先、A10G 备选。此前欧洲区 L4 无空闲时请求一直排队，报告停在专家步骤直至超时。公网入口 `eu-west` 不变；专家推理可能在欧洲以外执行，见部署说明。

## [0.3.2] - 2026-10-30

### 修复

- Modal 专家端点的长请求会每约 150 秒返回一次 `303`；transport 此前只跟随一跳，专家调用超过约 300 秒就被记为结果未知，报告挂起。现在在原超时内逐跳取回同一尝试的结果，每跳都校验同源、同路径和唯一 `__modal_attempt_token`，最多 24 跳，不重发 `POST`。

### 安全

- 后端运行依赖新增 `certifi>=2026.10.19`，与 2026-10 CA 泄露事件后更新的根证书库保持一致；部署镜像同时更新系统 `ca-certificates`。

### 已知限制

- 页面上的"确认重试"只放行本次运行请求账本中的未知请求，不能解除共享费用账本中的未结算 attempt；须由操作者按[部署说明](docs/deployment.md#未知费用请求)确认。

## [0.3.1] - 2026-10-07

### 安全

- 前端构建依赖 `source-map-js` 升级到 `1.2.2`，修复索引式 source map 偏移可导致事件循环拒绝服务的问题；该依赖仅用于开发与构建，不属于生产后端依赖。

### 修复

- 历史图片、视频预览通过短期 HttpOnly 媒体 Cookie 保持账户鉴权，视频保留 HTTP Range；关闭或切换会话丢弃迟到响应。
- 会话及管理员删除统一清理服务器可信归属的上传、中间产物和历次报告文件。清理失败保留数据库归属供重试，删除后迟到的识别与报告写回不能复活文件。
- 上传接口兼容可选 `session_id`，新工作台将每批上传直接归入会话目录；旧工作区升级提供默认只核查、无冲突才补归属的脚本。

## [0.3.0] - 2026-09-25

### 安全

- `/api/v1/inputs/generate-from-upload` 需要登录。此前可以匿名上传文件并触发视觉模型。
- 上传清单的 `category_id` 只接受单级路径段（字母、数字、点、下划线、连字符），服务端写入前再确认目标目录位于本次上传目录内。此前构造的分组标识可以把上传文件写到上传目录之外。
- `/api/v1/inputs/generate-from-video` 以及报告接口的 `input_path`、`video_path` 仅限管理员。此前任何登录用户都能在服务目录范围内引用其他用户的资料。
- `/api/v1/ready` 无需登录，现在只返回各项是否就绪及提示，不再返回异常详情、服务器路径、内部服务地址与模型名；完整结果写入后端日志。
- 按 trace_id 定位报告输出目录时要求其为单级路径段。
- 前端会话 ID 与生产烟测账号口令改用安全随机数。

### 不兼容变更

- `/api/v1/reports/generate` 与 `/api/v1/reports/generate/stream` 必须登录。未开启报告 Harness 的部署此前允许匿名调用，并会使用系统模型端点。
- 普通用户不能再用 `input_path`、`video_path` 提交报告或生成事故信息，改用上传接口；管理员与命令行不受影响。
- 上传清单中不符合上述规则的 `category_id` 返回 400。仓库前端使用的八个分组标识均符合规则。

## [0.2.1] - 2026-09-25

### 修复

- 测试专用依赖 `pytest`、`pypdf` 移至新的 `backend/requirements-dev.txt`。0.2.0 把 `pypdf` 列为运行依赖，基于已核验运行镜像的 `backend.runtime.Dockerfile` 构建会在依赖核验步骤失败；开发与 CI 改为安装 `requirements-dev.txt`。

## [0.2.0] - 2026-09-25

### 变更

- 提示词模板改用英文文件名：`input_generation_prompt.md`、`guidance_prompt.md`、`report_prompt.md`。配置中的旧中文路径在加载时自动映射，已部署的配置无需修改。
- 前端官方构建镜像升级到 Node.js 22；文档中的最低版本更正为 Python 3.11、Node.js 22.22。
- 依赖升级：前端 vitest 5、@vitejs/plugin-react 6、vite 8.3；前端运行镜像 nginx 1.29；Rust 构建镜像 1.98。
- 前端类型检查不再在源码目录生成 `vite.config.js`、`*.tsbuildinfo` 等文件。

### 其他

- CI 在 Python 3.11、3.12、3.13 上运行后端测试；新增 Dependabot 依赖更新与行为准则。
- CI 构建后端与前端 Docker 镜像；Rust 加速模块按 `Cargo.lock` 构建；Docker 构建上下文排除本地环境文件、知识库与工具目录。

## [0.1.0] - 2026-09-25

首个带版本号的公开发布。

### 功能

- 事故资料工作台：图片、视频分组上传，结构化事故信息生成、编辑与保存，会话恢复。
- 视频链路：YOLO + ByteTrack + 自适应抽帧 + 视觉模型（可选依赖）。
- 专家指导意见、稀疏与稠密混合检索（RRF）、检索增强报告生成，Markdown / Word / PDF 导出。
- 报告 Harness：证据快照、独立审查与修订、预算与费用门、审计与恢复；支持正式与演示两种发布模式。
- 账户体系：用户名密码登录与注册、管理员与普通用户角色、按用户隔离的会话与模型配置、报告质量反馈与导出。
- 同机与分机两套部署模板，以及专家模型的 Modal 部署脚本。

### 不兼容变更

- 管理员初始口令不再有默认值：服务器配置下 `BOOTSTRAP_ADMIN_PASSWORD` 为空、少于 8 位或为曾公开的默认口令时后端拒绝启动；本地开发不设置时跳过创建管理员。
- 服务器配置的专家模型地址不再指向任何具体部署，必须通过 `EXPERT_LOCAL_BASE_URL` 指定。
- 部署脚本改为按用途命名：`provision-212.sh` → `provision-app-host.sh`，`mount-213-kbase.sh` / `mount-213-runtime.sh` → `mount-remote-kbase.sh` / `mount-remote-runtime.sh`，`prepare-213-release.py` → `prepare-single-host-release.py`，`nginx.213.site.conf` → `nginx.host-site.conf`，`renew-213-nginx.sh` → `renew-host-nginx.sh`。
- `production-workspace-smoke.mjs` 不再有默认目标站点，必须设置 `PRODUCTION_SMOKE_URL`。
- 报告端点收敛为单一端点，移除 `max / pro / lite` 档位及对应的 `LITE_MODEL_*`、`DUCKCODING_API_KEY` 变量。

### 其他

- 新增 CI（后端测试、前端测试与构建、提交历史密钥扫描）、贡献指南、安全策略与 issue / PR 模板。
- 在 README 与 NOTICE 中说明可选视频依赖 Ultralytics YOLO 的 AGPL-3.0 许可。

[未发布]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.14...HEAD
[0.3.14]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.13...v0.3.14
[0.3.13]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.12...v0.3.13
[0.3.12]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.11...v0.3.12
[0.3.11]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.10...v0.3.11
[0.3.10]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/QianYan-Art/SafetyRAISE-OS/releases/tag/v0.1.0
