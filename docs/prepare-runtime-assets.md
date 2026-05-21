# 运行时资产说明

本文档说明 SafetyRAISE 在首次联调或部署前需要准备的模型、服务、权重和知识库文件，并给出默认值、放置位置和配置入口。

## 配置入口

运行时资产相关配置主要集中在以下位置：

1. [.env.example](/D:/MCP_Server/TS_analysis_report/.env.example)
2. [backend/config/workflow.yaml](/D:/MCP_Server/TS_analysis_report/backend/config/workflow.yaml)
3. [backend/config/workflow.server.yaml](/D:/MCP_Server/TS_analysis_report/backend/config/workflow.server.yaml)

推荐使用方式：

1. 本地联调时，复制 `.env.example` 为 `.env` 或直接在终端设置环境变量
2. 服务器部署时，复制 `.env.example` 为 `.env.server`
3. 仅在需要修改默认结构时再调整 `workflow.yaml` 或 `workflow.server.yaml`

## 默认模型与服务

### 专家模型

用途：生成专家指导意见，作为报告链路的前置分析节点。

默认值：

```text
EXPERT_LOCAL_MODEL=suyuan37/SafetyRAISE-TS-Qwen3
EXPERT_LOCAL_BASE_URL=http://127.0.0.1:1234/v1
```

模型仓库：

<https://huggingface.co/suyuan37/SafetyRAISE-TS-Qwen3>

接口要求：

```text
POST /v1/chat/completions
```

说明：

1. 程序里填写的是推理服务实际加载后的模型名
2. 不是直接请求 Hugging Face 页面
3. 可以由 LM Studio、vLLM、Ollama 兼容服务或其他 OpenAI 兼容服务承载

### YOLO 检测模型

用途：视频链路中的目标检测与轨迹提取。

默认值：

```text
YOLO_MODEL_PATH=models/yolo11n.pt
```

权重来源：

1. Ultralytics YOLO11 文档：<https://docs.ultralytics.com/models/yolo11/>
2. 权重下载：<https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt>

默认放置位置：

```text
models/yolo11n.pt
```

### Embedding 模型

用途：将查询和知识片段转为向量，供 `hybrid_local` 检索使用。

默认值：

```text
RETRIEVAL_EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b
RETRIEVAL_EMBEDDING_BASE_URL=http://127.0.0.1:1234/v1
```

推荐模型来源：

<https://huggingface.co/Qwen/Qwen3-Embedding-0.6B>

接口要求：

```text
POST /v1/embeddings
```

说明：

1. 程序请求的是一个 OpenAI 兼容 embeddings 服务
2. 仅下载模型文件还不够，必须把模型挂成服务
3. 如果更换 embedding 模型，需要同步重建 dense 索引

### Reranker 模型

用途：对召回的知识片段重新排序。

默认值：

```text
RETRIEVAL_RERANKER_MODEL=Alibaba-NLP/gte-multilingual-reranker-base
RETRIEVAL_RERANKER_BASE_URL=http://127.0.0.1:8081
```

推荐模型来源：

<https://huggingface.co/Alibaba-NLP/gte-multilingual-reranker-base>

接口要求：

```text
GET /health
POST /rerank
```

说明：

1. 服务器部署默认可直接使用 Compose 内置的 `retrieval-reranker` 容器
2. 本地联调时也可以自行起一个兼容 `/rerank` 的服务

### 视觉模型

用途：根据图片和关键帧生成事故信息草稿。

默认配置位于：

1. [backend/config/workflow.yaml](/D:/MCP_Server/TS_analysis_report/backend/config/workflow.yaml) `models.accident_vision`
2. [backend/config/workflow.server.yaml](/D:/MCP_Server/TS_analysis_report/backend/config/workflow.server.yaml) `models.accident_vision`

默认形态：远端 OpenAI 兼容视觉 API。

### 报告生成模型

用途：生成最终事故分析报告正文。

默认配置位于：

1. [backend/config/workflow.yaml](/D:/MCP_Server/TS_analysis_report/backend/config/workflow.yaml) `models.report_external`
2. [backend/config/workflow.server.yaml](/D:/MCP_Server/TS_analysis_report/backend/config/workflow.server.yaml) `models.report_external`

默认形态：远端 OpenAI 兼容 API。

默认存在三个可切换端点：

| 前端档位 | 默认端点名 | 默认模型 | 主要配置位置 |
| --- | --- | --- | --- |
| `pro` | `openrouter_kimi` | `moonshotai/kimi-k2.5` | `models.report_external.endpoints[openrouter_kimi]` |
| `lite` | `lite_compatible` | `qwen2.5-7b-instruct` | `models.report_external.endpoints[lite_compatible]` + `LITE_MODEL_*` |
| `max` | `duckcoding_gemini31` | `gemini-3.1-pro-preview` | `models.report_external.endpoints[duckcoding_gemini31]` |

程序约束：

1. 前端只显示 `max / pro / lite`
2. 后端要求每个端点都带唯一的 `selector_label`
3. `selector_label` 只能是 `max`、`pro`、`lite`
4. 同一时间只会选中其中一个端点生成报告

如果要替换报告模型，直接改对应端点即可：

1. 改 `pro`：修改 `openrouter_kimi`
2. 改 `lite`：修改 `lite_compatible` 和 `LITE_MODEL_*`
3. 改 `max`：修改 `duckcoding_gemini31`

如果只是替换供应商，不想改前端文案，不要改 `selector_label`，只改 `name`、`url`、`model`、`api_key_env` 或 `connection`。

## 需要准备的文件和服务

### 必需项

首次跑通主链路，至少需要准备：

1. 一个可用的专家模型服务
2. 视觉模型 API
3. 至少一个可用的报告模型端点
4. 基础知识库三件套

### 视频链路额外项

启用视频链路还需要：

1. `ffmpeg`
2. `ffprobe`
3. `models/yolo11n.pt`

### `hybrid_local` 额外项

启用默认的 `hybrid_local` 检索还需要：

1. Embedding 服务
2. Reranker 服务
3. Dense 索引三件套

## 知识库文件

### `local_jsonl` 与 `hybrid_local`

`local_jsonl`：

1. 只依赖本地 JSON/JSONL 文件
2. 可以先跑通最基础的检索链路
3. 不要求 Embedding 服务和 Dense 索引

`hybrid_local`：

1. 在 `local_jsonl` 基础上增加向量召回和 reranker
2. 需要 Embedding 服务、Reranker 服务和 Dense 索引文件
3. 是仓库默认检索模式

首次联调如果还没有 Embedding 或 Dense 索引，可以先把：

```yaml
retrieval:
  provider: "local_jsonl"
```

写到本地配置里，等基础知识库验证通过后再切回 `hybrid_local`。

### 基础知识库三件套

默认路径：

```text
kbase/data/manifest.json
kbase/data/kbase_chunks.jsonl
kbase/data/liability_rules.jsonl
```

仓库样例：

1. [examples/kbase/minimal/manifest.json](/D:/MCP_Server/TS_analysis_report/examples/kbase/minimal/manifest.json)
2. [examples/kbase/minimal/kbase_chunks.jsonl](/D:/MCP_Server/TS_analysis_report/examples/kbase/minimal/kbase_chunks.jsonl)
3. [examples/kbase/minimal/liability_rules.jsonl](/D:/MCP_Server/TS_analysis_report/examples/kbase/minimal/liability_rules.jsonl)

文件职责：

1. `manifest.json`：记录知识库版本、生成时间和来源信息
2. `kbase_chunks.jsonl`：案例片段、法条摘要、标准条文等通用知识块
3. `liability_rules.jsonl`：责任划分规则

### Dense 索引三件套

默认路径：

```text
kbase/data/dense_manifest.json
kbase/data/dense_records.jsonl
kbase/data/dense_vectors.f16.npy
```

文件职责：

1. `dense_manifest.json`：记录 embedding 模型版本和索引元数据
2. `dense_records.jsonl`：参与向量召回的记录
3. `dense_vectors.f16.npy`：与 `dense_records.jsonl` 一一对应的向量矩阵

要求：

1. `dense_records.jsonl` 的记录顺序必须和 `dense_vectors.f16.npy` 的向量行顺序一致
2. 更换 embedding 模型后必须重建这三份文件

## 知识库内容格式

### `manifest.json`

要求：合法 JSON 对象。

建议字段：

1. `generated_at`
2. `catalog_meta.catalog_version`
3. `sources`

### `kbase_chunks.jsonl`

要求：UTF-8 编码，一行一个 JSON 对象。

建议字段：

1. `chunk_id`
2. `source_id`
3. `title`
4. `content`
5. `category`
6. `tags`
7. `url`

### `liability_rules.jsonl`

要求：UTF-8 编码，一行一个 JSON 对象。

建议字段：

1. `rule_id`
2. `source_id`
3. `title`
4. `content`
5. `rule_type`
6. `scenarios`
7. `liability_subjects`
8. `authority`

### 检索真正会用到的字段

当前代码会直接利用以下字段参与检索和排序：

1. `content`
2. `title`
3. `category`
4. `rule_type`
5. `tags`
6. `scenarios`
7. `liability_subjects`
8. `chunk_id` / `rule_id` / `source_id`

## 首次联调顺序

推荐顺序如下：

1. 安装 Python、Node.js、ffmpeg、ffprobe
2. 下载 `yolo11n.pt` 到 `models/`
3. 准备专家模型服务，确认 `/v1/chat/completions` 可用
4. 准备最小知识库三件套
5. 本地先切到 `local_jsonl`
6. 跑通 `/api/v1/health`
7. 检查 `/api/v1/ready`
8. 接入视觉模型和报告模型 API
9. 准备 Embedding 服务、Reranker 服务和 Dense 索引
10. 切回 `hybrid_local`

## 常见改动位置

### 本地联调常改

1. `EXPERT_LOCAL_MODEL`
2. `EXPERT_LOCAL_BASE_URL`
3. `RETRIEVAL_EMBEDDING_BASE_URL`
4. `RETRIEVAL_EMBEDDING_MODEL`
5. `RETRIEVAL_RERANKER_BASE_URL`
6. `RETRIEVAL_RERANKER_MODEL`
7. `YOLO_MODEL_PATH`
8. `KBASE_*`
9. `KBASE_DENSE_*`

### 服务器部署常改

1. `.env.server`
2. `backend/config/workflow.server.yaml`
3. `deployment/docker/docker-compose.server.yml`

## 开源仓库包含与不包含的内容

开源仓库包含：

1. 默认配置
2. 读取逻辑
3. 最小知识库样例
4. 部署骨架

开源仓库不包含：

1. 私有知识库内容
2. YOLO 权重文件
3. Embedding 模型权重
4. Reranker 模型权重
5. 真实 API Key
