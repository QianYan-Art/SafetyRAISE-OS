# 贡献指南

感谢你愿意改进 SafetyRAISE。提交 issue 或 pull request 前，请先阅读本文；参与本项目即表示同意遵守[行为准则](CODE_OF_CONDUCT.md)。

## 开发环境

按 [快速开始](docs/quickstart.md) 安装后端与前端依赖。完整报告流程需要你自己的模型服务和非空知识库；只改界面、接口或测试时，一般不需要它们。

## 运行测试

提交前请至少运行与改动相关的测试，涉及公共行为时运行完整测试：

```sh
# 后端（完整测试需要回环地址上的独立测试库，见 README 的“验证”一节）
.venv/bin/python -m pytest -q backend/tests

# 前端
cd frontend
npm test
npm run build
```

浏览器测试（`frontend/tests/*.mjs`）用合成数据拦截接口，不调用真实模型，说明见 [前端工作台](docs/frontend-workbench.md) 和 [报告 Harness](docs/report-harness.md)。

pull request 会自动运行 CI（Python 3.11–3.13 后端测试、前端测试与构建、提交历史中的密钥扫描），全部通过后才会被合并。依赖更新由 Dependabot 每周提交。

## 不要提交的内容

- 任何真实凭据：API Key、数据库口令、JWT 密钥、SSH 私钥、`.env.server` 等。示例只写变量名或占位符。
- 真实事故材料（图片、视频、当事人信息）以及由它们生成的报告。
- 知识库正文、索引和向量文件；仓库只保留 `examples/kbase/minimal/` 这样无正文的结构模板。
- 模型权重、构建产物、运行输出和本地工具目录（已在 `.gitignore` 中列出）。

如果不慎提交了凭据，请先在对应服务中吊销或轮换它，再清理提交；只删除文件无法从 Git 历史中移除。

## 提交与 pull request

- 提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/)，例如 `fix(frontend): ...`、`feat(report): ...`、`docs: ...`，一次提交只做一件事。
- pull request 说明改动动机、影响范围和验证方式；修复缺陷时补充能复现问题的测试。
- 修改配置字段、接口或部署方式时，同步更新 `docs/` 中对应的文档，并在 [CHANGELOG](CHANGELOG.md) 的 “未发布” 一节记录。
- 代码和文档沿用现有风格：后端 Python 类型标注，前端 TypeScript；界面样式只使用 `frontend/src/styles.css` 中的设计令牌。

## 报告安全问题

请不要在公开 issue 中披露安全漏洞，处理方式见 [安全策略](SECURITY.md)。
