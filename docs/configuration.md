# 配置说明

## 配置文件分工

如果你当前的问题是“模型要下载什么、模型端点该改哪里、知识库文件应该长什么样”，先看 [运行资产准备](prepare-runtime-assets.md)；本文更偏向解释配置字段本身。

### `backend/config/workflow.yaml`

本地开发默认配置。后端如果没有显式指定 `WORKFLOW_CONFIG_PATH`，就会读取这份文件。

适合放在这里的内容：

1. 本地开发默认值
2. 通用上传限制
3. 通用工作流参数
4. 非敏感的模型端点示例

### `backend/config/workflow.server.yaml`

服务器部署示例配置。它**不是自动 merge 到 `workflow.yaml`**，而是通过环境变量：

```text
WORKFLOW_CONFIG_PATH
```

显式切换过去。

在 Docker Compose 示例里，backend 容器就是通过 `WORKFLOW_CONFIG_PATH=/app/backend/config/workflow.server.yaml` 使用这份配置。

适合放在这里的内容：

1. 容器内路径
2. 宿主机模型服务访问地址
3. sidecar 地址
4. 生产环境下更保守的默认值

## 环境变量占位符

配置文件中的字符串支持：

```text
${ENV_NAME:-default_value}
```

加载顺序是：

1. 读取 YAML
2. 展开环境变量占位符
3. 再做 Pydantic 校验

所以如果某项写成：

```yaml
base_url: "${RETRIEVAL_EMBEDDING_BASE_URL:-http://127.0.0.1:1234/v1}"
```

那么当前进程里若存在 `RETRIEVAL_EMBEDDING_BASE_URL`，就会覆盖默认值。

## 报告模型说明

报告生成端点已收敛为**单一端点**（默认 `openrouter_deepseek_v4_pro` → `deepseek/deepseek-v4-pro`）。`max/pro/lite` 档位与 `selector_label` 已下线。

- 系统默认报告端点 = `report_external.endpoints` 中按 `priority` 排在首位的端点。
- 视觉 / 嵌入重排 / 报告模型按「每用户能力配置」（`user_capability_configs`）解析：用户在前端「模型接入设置」里填 `url + key + model`，留空时仅嵌入回退系统默认，视觉/报告必须由普通用户自行填写（管理员留空则用系统默认，便于测试）。

`report_external.endpoints` 约束：

1. 至少保留一个端点
2. 每个端点都要有唯一的 `name`
3. 替换报告供应商：改该端点的 `name`、`url`、`model`、`api_key_env` 或 `connection`

## 关键环境变量分组

### 1. 专家模型

| 变量 | 作用 |
| --- | --- |
| `EXPERT_LOCAL_MODEL` | 专家模型名称 |
| `EXPERT_LOCAL_BASE_URL` | 专家模型服务地址 |
| `EXPERT_LOCAL_API_KEY_ENV` | 若服务端需要鉴权，指向真实 key 的环境变量名 |

### 2. lite 档位模型（已下线）

`lite` 报告档位与 `LITE_MODEL_*` 环境变量已随档位机制移除，报告端点收敛为单一远端端点。此小节仅作历史保留，新部署无需配置 `LITE_MODEL_*`。

### 3. 报告 / 视觉模型

| 变量 | 作用 |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter key |
| `DUCKCODING_API_KEY` | DuckCoding key |

说明：

1. `OPENROUTER_API_KEY` 默认同时服务于 `pro` 报告端点和视觉模型端点
2. `DUCKCODING_API_KEY` 默认服务于 `max` 报告端点
3. 如果你给 `lite` 档接入远端服务，优先使用 `LITE_MODEL_API_KEY`

### 4. embedding / reranker

| 变量 | 作用 |
| --- | --- |
| `RETRIEVAL_EMBEDDING_BASE_URL` | embedding 服务地址 |
| `RETRIEVAL_EMBEDDING_MODEL` | embedding 模型名 |
| `RETRIEVAL_EMBEDDING_API_KEY_ENV` | embedding key 环境变量名 |
| `RETRIEVAL_RERANKER_BASE_URL` | reranker sidecar 地址 |
| `RETRIEVAL_RERANKER_MODEL` | reranker 模型名 |

### 5. 知识库与 YOLO

| 变量 | 作用 |
| --- | --- |
| `KBASE_MANIFEST_PATH` | manifest 路径 |
| `KBASE_CHUNKS_PATH` | 通用知识片段路径 |
| `KBASE_RULES_PATH` | 责任规则路径 |
| `KBASE_SEARCH_INDEX_PATH` | 稀疏搜索索引路径 |
| `KBASE_DENSE_MANIFEST_PATH` | dense manifest 路径 |
| `KBASE_DENSE_RECORDS_PATH` | dense 记录路径 |
| `KBASE_DENSE_VECTORS_PATH` | dense 向量路径 |
| `YOLO_MODEL_PATH` | YOLO 权重路径 |

## 检索配置

当前默认检索器是：

```text
hybrid_local
```

由三部分组成：

1. 稀疏召回
2. dense 向量召回
3. reranker 重排

如果你只有基础知识片段和责任规则，没有 dense 索引产物，建议先切到：

```text
local_jsonl
```

对应的最小文件格式见 [运行资产准备](prepare-runtime-assets.md)。

关键参数在 `retrieval` 节点下：

1. `top_k`
2. `min_score`
3. `local_jsonl.*`
4. `hybrid.*`
5. `agentic.*`

其中要特别注意：

1. 如果更换 embedding 模型，必须重建 dense 索引文件。
2. `fallback_mock_on_error` 适合本地调试，不适合严肃部署场景。
3. `agentic.max_rounds` 越大，报告模型自主补检索的成本越高。

## 上传限制

上传限制集中在：

```text
input_generation.upload
```

当前默认值：

| 配置项 | 默认值 |
| --- | --- |
| `max_files` | `140` |
| `max_total_bytes` | `1073741824` |
| `max_image_bytes` | `10485760` |
| `max_video_bytes` | `104857600` |
| `max_model_images` | `48` |
| `max_images_per_group` | `20` |
| `max_videos_per_group` | `5` |
| `max_total_images` | `120` |
| `max_total_videos` | `20` |

如果你修改这些值，需要同时评估：

1. 前端提示是否匹配
2. 显存 / 内存压力是否还能接受
3. 视觉模型输入上限是否需要一起改

## 哪些更适合本地开发

更适合放在 `workflow.yaml` 的内容：

1. 本地路径
2. 本地模型服务地址
3. 调试时保守的上传规模
4. 调试期允许的 fallback 行为

## 哪些更适合服务器部署

更适合放在 `workflow.server.yaml` 或 `.env.server` 的内容：

1. 容器内路径
2. sidecar 地址
3. 宿主机模型服务地址
4. 生产域名、证书目录、挂载目录
5. 不应入库的密钥
