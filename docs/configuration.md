# 配置说明

## 配置文件分工

### 报告运行服务端配置

`report_harness` 缺省不改变旧流程。`enabled` 控制新数据接口，`online_enabled`
控制主应用是否装配正式业务运行，均默认 false，不能由客户端请求打开。

- `release_mode`：`formal`（默认）要求批准表存在当前版本的已批准绑定；`demo` 不读批准表，报告固定为工程稿并带标记导出。
- `runtime_manifest_path`：正式运行 manifest 的服务端绝对路径；引用既有账本和四角色合同，不创建新预算。
  manifest 由 `python -m app.report_harness.provision` 生成，知识库或模型配置变化后须重新生成。
- `resource_paths`：需要检查可用空间的应用与数据目录绝对路径列表，账本所在目录自动加入。
- `minimum_free_mib`：默认2048，最小256；只限制新增重任务，不删除文件腾空间。
- `minimum_memory_mib`：默认256，最小64；检查宿主及受支持的容器内存余量。

启用在线模式还要求数据库迁移；`formal` 另需代码校验清单与只读发布批准匹配。代码、批准、知识或合同
装配失败则拒绝启动，不静默降级为不受控模型调用。仅资源不足或资源探针失败时允许保留
查看和取消能力，关闭新增生成；运行中也在执行及外发前复核资源。
公开配置仅返回能力开关，不返回 manifest、模型密钥或账本位置。部署条件见 `docs/deployment.md`。

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

### 提示词模板

| 文件 | 配置字段 | 用途 |
| --- | --- | --- |
| `backend/config/input_generation_prompt.md` | `input_generation.prompt_path` | 从图片、视频抽帧和 YOLO 摘要生成结构化事故信息 |
| `backend/config/guidance_prompt.md` | `prompts.guidance_prompt_path` | 专家模型生成指导意见 |
| `backend/config/report_prompt.md` | `prompts.report_prompt_template` | 检索增强的分析报告生成 |

模板中的占位内容由后端按原样替换，删改占位内容会在运行时报错；`backend/tests/unit/test_report_prompt_contract.py` 检查报告模板的静态约定。0.1.0 及更早版本使用中文文件名，旧配置中的这些路径在加载时会自动映射到新文件名。

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

报告生成使用**单一端点**（默认端点名 `openrouter_primary`，默认模型 `tencent/hy4-preview`）。

默认报告模型使用 `reasoning.effort=high`，默认视觉模型 `openai/gpt-5.6-luna` 使用 `reasoning.effort=max`；
用户未在能力配置中选择推理等级时沿用该默认值。报告与视觉请求不发送 `max_tokens`、`max_completion_tokens` 或 `max_output_tokens`；指定推理等级时不同时发送 `reasoning.max_tokens`。旧配置字段仍可解析，但不作为输出截断参数。供应商容量仍用于 harness 内部费用预留，费用、轮数和未知请求保护不因此取消。

- 系统默认报告端点 = `report_external.endpoints` 中按 `priority` 排在首位的端点。
- 视觉 / 嵌入 / 报告模型按「每用户能力配置」（`user_capability_configs`）解析：用户在前端「模型接入设置」里填 `url + key + model`，视觉 / 报告可另选推理等级；普通用户嵌入按本人配置→首个启用管理员配置→系统默认解析，本人有配置时不再回退；视觉/报告必须由普通用户自行填写（管理员留空则用系统默认，便于测试）。

`report_external.endpoints` 约束：

1. 至少保留一个端点
2. 每个端点都要有唯一的 `name`
3. 替换报告供应商：改该端点的 `name`、`url`、`model`、`api_key_env` 或 `connection`

## 关键环境变量分组

服务器部署时这些变量写在仓库根目录的 `.env.server`（由 [`.env.example`](../.env.example) 复制而来，已被 Git 忽略）。

### 1. 数据库与鉴权

| 变量 | 作用 |
| --- | --- |
| `DATABASE_DSN` | PostgreSQL 连接串；同机部署用 Docker 网络内的服务名，分机部署用数据库主机地址 |
| `AUTH_JWT_SECRET` | 登录令牌签名密钥，使用高熵随机值（如 `openssl rand -hex 32`） |
| `BOOTSTRAP_ADMIN_USERNAME` | 首次启动自动创建的管理员用户名，默认 `safetyraise` |
| `BOOTSTRAP_ADMIN_PASSWORD` | 该管理员的初始口令，至少 8 位，没有默认值 |
| `BOOTSTRAP_ADMIN_DISPLAY_NAME` | 管理员显示名称 |

`workflow.server.yaml` 设置了 `auth.require_strong_secret: true`：`AUTH_JWT_SECRET` 为空或为公开默认串、`BOOTSTRAP_ADMIN_PASSWORD` 为空、少于 8 位或等于曾公开的默认口令时，后端拒绝启动。本地开发配置不做该检查；未设置管理员口令时只跳过创建管理员并记录警告，可先注册普通用户使用。管理员只在数据库中不存在同名用户时创建，之后修改口令请在管理控制台中进行。

### 2. 专家模型

| 变量 | 作用 |
| --- | --- |
| `EXPERT_LOCAL_PROVIDER` | 专家模型提供器；服务器默认 `openai_compatible`，本地 LM Studio 可显式覆盖 |
| `EXPERT_LOCAL_MODEL` | 专家模型名称 |
| `EXPERT_LOCAL_BASE_URL` | 专家模型服务地址；服务器配置没有可用默认值，必须显式设置 |
| `EXPERT_LOCAL_API_KEY_ENV` | 若服务端需要鉴权，指向真实 key 的环境变量名 |
| `MODAL_EXPERT_PROXY_TOKEN` | 用 `deployment/modal/qwen3_expert.py` 部署时的 Proxy Auth 裸 token；程序统一添加 `Bearer` 前缀 |

服务器配置把专家模型固定为系统级能力：普通用户和管理员都不能在模型接入设置中查看或修改该端点，也不能从公开 readiness、报告响应或授权预览中取得地址和凭据。`workflow.server.yaml` 默认给单次专家请求 `1800` 秒超时，用于覆盖按需 GPU 冷启动和完整的一轮生成；请求体不发送 `max_tokens`、`max_completion_tokens` 或 `max_output_tokens`。

自动重试只用于明确的连接、写入、协议中断、无效 JSON 或可重试 HTTP 状态。已经进入读取阶段但超时的请求不自动重发，避免同一台单并发专家服务同时生成两份结果。Modal 冷启动返回的同次尝试恢复由 Harness transport 单独处理，规则见 [报告 Harness](report-harness.md)。

### 3. 报告 / 视觉模型

| 变量 | 作用 |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter key |

说明：

1. `OPENROUTER_API_KEY` 默认同时服务于单一报告端点（`openrouter_primary`）、视觉模型端点与嵌入模型
2. 用户在前端自填 `url + key + model` 时只需填到 `/v1`，系统自动补全 `/chat/completions`（报告/视觉）或 `/embeddings`（嵌入）
3. 视觉 / 报告可另选推理等级，存于 `user_capability_configs.params.reasoning_effort`：留空沿用上述系统默认；
   选定 `none / minimal / low / medium / high / xhigh / max` 之一时按所选等级发送；选 `off` 时请求体不携带推理参数，
   供不支持该参数的上游使用。系统不探查上游能力，也不自动升降等级；该项对嵌入用途不开放

### 4. embedding / reranker

| 变量 | 作用 |
| --- | --- |
| `RETRIEVAL_EMBEDDING_BASE_URL` | embedding 服务地址 |
| `RETRIEVAL_EMBEDDING_MODEL` | embedding 模型名；须与构建稠密索引时一致，默认 `qwen/qwen3-embedding-8b`（4096 维） |
| `RETRIEVAL_EMBEDDING_API_KEY_ENV` | embedding key 环境变量名 |
| `RETRIEVAL_RERANKER_BASE_URL` | reranker sidecar 地址 |
| `RETRIEVAL_RERANKER_MODEL` | reranker 模型名 |
| `RETRIEVAL_RERANKER_API_KEY_ENV` | reranker key 环境变量名，sidecar 不需要鉴权时留空 |

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

默认由两类召回通过 RRF 融合：

1. 稀疏召回
2. dense 向量召回

`reranker.enabled` 默认关闭。只有显式启用 reranker 时，系统才会在融合结果上继续调用 reranker 重排；未启用时不要求部署 reranker 服务。

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
3. `agentic.max_rounds` 越大，报告模型自主补检索的成本越高（服务器默认 3 轮、`max_total_snippets` 12；知识库较小时可下调）。

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
