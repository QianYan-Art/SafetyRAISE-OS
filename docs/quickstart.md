# 快速开始

## 环境要求

本地开发至少准备：

1. Python `3.11+`（CI 覆盖 3.11–3.13，官方镜像为 3.12）
2. Node.js `22.22+`（前端构建与测试依赖的最低版本）
3. `uv` 或可用的 Python 虚拟环境工具
4. ffmpeg / ffprobe
5. 可选：GPU 与 CUDA，用于更快的视频链路
6. PostgreSQL，以及可访问的模型服务和非空知识库（完整报告流程）

如果你只想先跑前后端接口，不测视频链路，可以先不准备 YOLO 权重。

## 安装依赖

仓库根目录执行（Windows 下把 `.venv/bin/python` 换成 `.venv/Scripts/python.exe`，下同）：

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r backend/requirements-dev.txt
```

`requirements-dev.txt` 在运行时依赖 `requirements.txt` 之外加入测试工具；只部署运行时可只装后者。

如需启用视频链路，再安装（`ultralytics` 采用 AGPL-3.0，见 README 的许可说明）：

```sh
uv pip install --python .venv/bin/python -r backend/requirements-video.txt
```

安装前端依赖：

```sh
cd frontend
npm install
cd ..
```

## 准备配置

本地开发默认读取：

```text
backend/config/workflow.yaml
```

这个文件支持环境变量占位符，格式为：

```text
${ENV_NAME:-default_value}
```

推荐直接在当前终端注入环境变量，而不是把真实密钥写回仓库文件。

首次联调前，先读：

```text
docs/prepare-runtime-assets.md
```

示例（PowerShell 中写成 `$env:NAME="value"`）：

```sh
export OPENROUTER_API_KEY="your-openrouter-key"
export DATABASE_DSN="postgresql://<user>:<password>@127.0.0.1:5432/safetyraise"
export AUTH_JWT_SECRET="<replace-with-a-strong-random-secret>"
export BOOTSTRAP_ADMIN_PASSWORD="<replace-with-a-private-password>"
export EXPERT_LOCAL_MODEL="suyuan37/SafetyRAISE-TS-Qwen3"
export EXPERT_LOCAL_BASE_URL="http://127.0.0.1:1234/v1"
```

首次启动时，后端用 `BOOTSTRAP_ADMIN_USERNAME`（默认 `safetyraise`）和 `BOOTSTRAP_ADMIN_PASSWORD` 创建管理员。口令没有默认值：本地开发不设置时只跳过创建管理员，可以先注册普通用户；服务器配置下未设置或不安全会拒绝启动。

## 准备知识库

默认相对路径如下：

```text
kbase/data/manifest.json
kbase/data/kbase_chunks.jsonl
kbase/data/liability_rules.jsonl
kbase/data/search_index.json
kbase/data/dense_manifest.json
kbase/data/dense_records.jsonl
kbase/data/dense_vectors.f16.npy
```

你可以：

1. 直接在仓库根目录放置 `kbase/data/...`
2. 或通过环境变量覆写：
   - `KBASE_MANIFEST_PATH`
   - `KBASE_CHUNKS_PATH`
   - `KBASE_RULES_PATH`
   - `KBASE_SEARCH_INDEX_PATH`
   - `KBASE_DENSE_MANIFEST_PATH`
   - `KBASE_DENSE_RECORDS_PATH`
   - `KBASE_DENSE_VECTORS_PATH`

如果这些文件缺失，`hybrid_local` 检索链路无法正常工作。

如果还没有 Embedding 服务或 Dense 索引，可以在**非空**知识库上先切到 `local_jsonl`，验证基础检索后再补 `hybrid_local`；Reranker 默认关闭。

仓库的 `examples/kbase/minimal/` 是**无知识正文**的结构模板，包含 `config/`、`data/`、`scripts/`、`source_documents/`。`data/manifest.json`、两个空 JSONL 与空倒排索引可被基础读取器解析，但检索必定没有结果；不能将其复制到正式知识库路径、生成报告或替代验收数据。运行真实流程时，从自己的合法来源建立非空 `kbase/data/`，不要将正文提交到 Git。

本地只调试稀疏检索时，把 `backend/config/workflow.yaml` 的 `retrieval.provider` 改为 `local_jsonl`：

   ```yaml
   retrieval:
     provider: "local_jsonl"
   ```

`local_jsonl` 依赖非空的 manifest、chunks 和 rules，不要求 `search_index.json`、Embedding 服务和 Dense 索引。可选倒排表支持完整 posting 或 `[id, tf]` 加 `doc_meta` 的紧凑格式。验证基础检索后，再用同一知识版本构建 Dense 索引并切回默认 `hybrid_local`；更换 embedding 模型须重建 Dense 索引。

## 准备 YOLO 与视频依赖

默认 YOLO 权重路径：

```text
models/yolo11n.pt
```

也可以通过环境变量 `YOLO_MODEL_PATH` 覆写。

另外确保以下命令可在 PATH 中找到：

```text
ffmpeg
ffprobe
```

## 准备模型服务

当前配置默认分成四类：

1. 专家模型：先生成结构化指导意见
2. 报告模型：生成最终分析报告
3. 视觉模型：生成事故草稿
4. embedding / reranker：支撑 hybrid 检索

默认配置里：

1. 专家模型走 OpenAI 兼容地址（如 LM Studio / vLLM 暴露的 `/v1`）
2. 报告模型走兼容 OpenAI 的远端端点（单一端点，默认 `tencent/hy4-preview`）
3. 视觉 / 嵌入端点同为 OpenAI 兼容
4. 用户在前端只需把地址填到 `/v1`，系统自动补全 `/chat/completions`（报告/视觉）或 `/embeddings`（嵌入）

最容易忽略的是：

1. embedding 不是“只下载模型文件”就结束，它必须真的提供 `/v1/embeddings`
2. reranker 不是“只填模型名”就结束，它必须真的提供 `/rerank`
3. 专家模型如果以后发布到 Hugging Face，程序里仍然要填“推理服务地址”，不是直接填网页链接

至少需要保证一条完整报告链路可用，否则前端虽可打开，但无法生成结果。

## 启动顺序

建议按以下顺序启动本地开发环境：

1. 在仓库根目录打开第一个终端，启动后端服务。
2. 在仓库根目录打开第二个终端，进入 `frontend` 目录后启动前端服务。
3. 后端启动后先执行健康检查，再打开浏览器访问前端页面。

## 启动后端

```sh
.venv/bin/python -m uvicorn app.main:app --app-dir backend --reload --port 8000
```

建议始终从仓库根目录启动这条命令，避免 Python 模块路径和相对配置路径偏移。

## 启动前端

```sh
cd frontend
npm run dev
```

默认访问地址通常是：

```text
http://localhost:5173
```

如果首页可以打开但接口请求全部失败，优先检查两项：

1. 后端进程是否仍在运行
2. 前端是否通过默认 `/api` 反代或正确的 `VITE_API_BASE` 指向后端

## 健康检查

后端启动后，可先检查：

```sh
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/ready
```

`/ready` 列出每项依赖是否就绪及提示，通常比直接从前端排查更快；它无需登录，因此只返回状态与提示，不返回异常详情、服务器路径、内部端点和模型名，未就绪时的完整结果写在后端日志的 warning 中。
`/ready` 仅在关键依赖（如知识库、YOLO 权重或必要模型端点）未就绪时返回 `503`。embedding 或 reranker 未准备好时，hybrid 检索可降级为 `sparse_only_fallback`；这两项本身不必然导致 `503`。

## 常见失败点

1. `环境变量未设置`
   - 说明配置里引用了某个 `api_key_env`，但当前终端没有该环境变量。
2. `知识库文件不存在`
   - 说明 `kbase/data/...` 没放好，或路径覆写错了。
3. `ffmpeg` / `ffprobe` 找不到
   - 视频链路无法初始化。
4. YOLO 权重不存在
   - 有视频上传时会失败。
5. `report.docx` / `report.pdf` 导出失败
   - 通常是缺少 `python-docx` 或 `reportlab`。

## 联调顺序

首次联调建议按以下顺序推进：

1. `/health` 正常
2. `/ready` 只剩你能接受的未就绪项
3. 先跑图片输入
4. 再加视频
5. 最后再压导出
