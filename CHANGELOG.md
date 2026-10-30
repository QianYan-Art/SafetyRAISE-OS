# 变更记录

本文件记录面向使用者和部署者的重要变化，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。`0.x` 期间次版本号变化可能包含不兼容改动。

## [未发布]

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

[未发布]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.3.10...HEAD
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
