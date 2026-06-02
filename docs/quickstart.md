# 快速开始

## 环境要求

本地开发至少准备：

1. Python `3.10+`
2. Node.js `18+`
3. `uv` 或可用的 Python 虚拟环境工具
4. ffmpeg / ffprobe
5. 可选：GPU 与 CUDA，用于更快的视频链路

如果你只想先跑前后端接口，不测视频链路，可以先不准备 YOLO 权重。

## 安装依赖

仓库根目录执行：

```powershell
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r backend/requirements.txt
```

如需启用视频链路，再安装：

```powershell
uv pip install --python .venv/Scripts/python.exe -r backend/requirements-video.txt
```

安装前端依赖：

```powershell
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

PowerShell 示例：

```powershell
$env:OPENROUTER_API_KEY="your-openrouter-key"
$env:DUCKCODING_API_KEY="your-duckcoding-key"
$env:LITE_MODEL_API_KEY=""
$env:EXPERT_LOCAL_MODEL="suyuan37/SafetyRAISE-TS-Qwen3"
$env:EXPERT_LOCAL_BASE_URL="http://127.0.0.1:1234/v1"
```

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

如果还没有 Embedding 服务、Reranker 服务或 Dense 索引，可以先用仓库自带的最小样例切到 `local_jsonl`，跑通基础检索后再补 `hybrid_local`。

仓库在 `examples/kbase/minimal/` 提供了一份最小知识库样例，零外部依赖即可跑通基础检索：

1. 把样例三件套拷到默认运行时目录：

   ```powershell
   New-Item -ItemType Directory -Force kbase/data | Out-Null
   Copy-Item examples/kbase/minimal/manifest.json        kbase/data/
   Copy-Item examples/kbase/minimal/kbase_chunks.jsonl   kbase/data/
   Copy-Item examples/kbase/minimal/liability_rules.jsonl kbase/data/
   ```

2. 把 `backend/config/workflow.yaml` 的 `retrieval.provider` 改为 `local_jsonl`：

   ```yaml
   retrieval:
     provider: "local_jsonl"
   ```

`local_jsonl` 只依赖上述三件套，不需要 `search_index.json`、Embedding 服务和 Dense 索引。验证基础检索通过后，再准备 Dense 索引并切回默认的 `hybrid_local`。

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

默认示例里：

1. 专家模型本地走 Ollama 风格地址
2. `lite` 档位走兼容 OpenAI 的本地或远端端点
3. `pro / max / 视觉` 端点保留公开可接入的示例配置

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

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload --port 8000
```

建议始终从仓库根目录启动这条命令，避免 Python 模块路径和相对配置路径偏移。

## 启动前端

```powershell
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

```powershell
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/ready
```

`/ready` 会直接暴露未就绪依赖，通常比直接从前端排查更快。
如果知识库、YOLO 权重、embedding、reranker 或模型端点尚未准备好，`/ready` 返回 `503` 属于预期现象。

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
