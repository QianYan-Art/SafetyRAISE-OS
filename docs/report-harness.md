# 报告 Harness

## 当前边界

本模块是报告证据与独立审查运行控制的开发实现，旧报告链路保持默认。
生产工厂默认关闭新模式，不能通过客户端字段选用合成角色或测试批准表。
服务端显式设置 `report_harness.enabled=true` 后可使用证据/运行数据接口，
但 `online_enabled` 仍必须为 false，实际模型执行保持关闭。旧配置缺少此配置组时行为不变。
合成角色只用于控制流测试，不能证明真实报告质量。

当前开发路径支持人工文字补证、冻结快照和受控的本地知识查询；在线 transport 尚未接入，
未完成的能力明确拒绝，不假装已执行。
候选、审查与发布正文存入专用 PostgreSQL 表，不写旧报告输出目录。
真实质量验收、模型外发、生产迁移和部署均不是运行测试的附带操作。

## 证据与授权

- 会话证据使用独立 GET/PUT `/api/v1/chat-sessions/{session_id}/report-evidence`，
  全量保存带 `expected_revision`；来源、核实状态、冲突和服务端审计字段共同保留。
- 创建 run 时在数据库事务内检查 revision 并冻结文本；之后的证据或草稿修改不会改变旧 run。
  草稿字段未就绪时保存返回警告，创建 run 时无效 JSON Pointer 必须拒绝。
- 所有者可读取 `/api/v1/report-runs/{run_id}/authorization-preview`；
  缺少完整的服务端端点描述或获批知识清单时明确不可批准，不返回密钥或自动探测端点。
- authorize 必须确认快照、端点配置和知识清单三个服务端摘要。
  授权与事实核实状态互相独立，不能由模型写入 approved，也不会增加预算。
- 当前即使授权成功，outbound 仍返回 `outbound_transport_unavailable`，
  待后续物理请求预算和取消发送屏障完成才可启用。测试批准仅使用合成端点。

## 受控上下文

生成和审查各自通过有界角色循环调用 `list_evidence`、`read_evidence`、
`search_knowledge`、`read_knowledge`。工具没有 shell、文件读取、URL 抓取、
修改事实、改变预算或批准发布的能力。知识来自服务端固定注入的原文集合，
不是模型 prepare 返回的新事实；创建运行时固定副本和摘要，配置变更拒绝执行旧运行。

大快照只给目录和必要事实义务，模型须回查原文。目录中的 ID 存在不代表已读；
发布检查分开校验生成者引用来源和审查者实际取得的证据/知识，语义支持仍由审查负责。
每次工具执行前后保存私有检查点；公开事件只包含受控工具名、状态和摘要。
生成者工具历史不会进入审查者上下文。逻辑工具次数上限不能替代物理请求费用账本。

## 独立数据库

测试只接受 `REPORT_HARNESS_TEST_DSN`。host 必须为回环地址，数据库名必须以
`safetyraise_harness_test` 开头。禁止读取业务 `.env`，缺少 DSN 是测试错误，不是跳过条件。

`backend/app/report_harness/migrations/001_report_runs.sql` 是显式版本迁移。
测试 fixture 仅在已确认的独立库建立最小旧表契约和新表，并清理自己创建的用户与运行记录；
不删除数据库、不操作已有业务用户或会话、不在生产服务启动时迁移。

## 验证

在主仓根目录执行：

```powershell
$env:PYTHONPATH = 'backend'
.\.venv\Scripts\python.exe -m pytest backend/tests/unit -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest backend/tests/test_report_run_store.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest backend/tests/test_evidence_contract.py backend/tests/test_outbound_approval.py -q -p no:cacheprovider
```

第一条验证纯逻辑，第二条必须连接真实独立 PostgreSQL。
纯逻辑通过、语法检查或角色调用次数不能代替数据库事务、并发及重启验证。
发布必须绑定最终候选与审查摘要，并同时满足义务覆盖、五类审查、无未关闭重大问题、
未取消和有效执行租约。

## 恢复与关闭

当前运行迁移是版本 1；版本不匹配拒绝使用新模式，不能降级成忽略持久化的执行。
取消使旧执行令牌失效；流式连接断开应取消而非后台继续。
未知请求恢复、全局物理请求预算和正式导出资格将在后续切片完成前保持关闭，
不得将本开发状态宣传为完整工程门或真实质量门通过。
