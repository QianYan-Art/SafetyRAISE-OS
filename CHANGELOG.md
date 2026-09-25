# 变更记录

本文件记录面向使用者和部署者的重要变化，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。`0.x` 期间次版本号变化可能包含不兼容改动。

## [未发布]

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

[未发布]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/QianYan-Art/SafetyRAISE-OS/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/QianYan-Art/SafetyRAISE-OS/releases/tag/v0.1.0
