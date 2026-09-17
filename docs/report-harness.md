# 报告 Harness

## 当前边界

本模块是报告证据与独立审查运行控制的开发实现，旧报告链路保持默认。
生产工厂默认关闭新模式，不能通过客户端字段选用合成角色或测试批准表。
服务端显式设置 `report_harness.enabled=true`，且schema及批准表只读权限验证通过后，
可使用证据/运行数据接口；
但 `online_enabled` 仍必须为 false，实际模型执行保持关闭。旧配置缺少此配置组时行为不变。
合成角色只用于控制流测试，不能证明真实报告质量。

当前开发路径支持人工文字补证、冻结快照和受控的本地知识查询；
计账 transport 已在隔离回环 HTTP 中集成验证，生产工厂尚未接入在线角色，
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
  待真实模型协议/预算上界验证及Q授权后才可适配启用。测试批准仅使用合成端点。

## 受控上下文

生成和审查各自通过有界角色循环调用 `list_evidence`、`read_evidence`、
`search_knowledge`、`read_knowledge`。工具没有 shell、文件读取、URL 抓取、
修改事实、改变预算或批准发布的能力。知识来自服务端固定注入的原文集合，
不是模型 prepare 返回的新事实；创建运行时固定副本和摘要，配置变更拒绝执行旧运行。

大快照只给目录和必要事实义务，模型须回查原文。目录中的 ID 存在不代表已读；
发布检查分开校验生成者引用来源和审查者实际取得的证据/知识，语义支持仍由审查负责。
每次工具执行前后保存私有检查点；公开事件只包含受控工具名、状态和摘要。
生成者工具历史不会进入审查者上下文。逻辑工具次数上限不能替代物理请求费用账本。

## 审查与修订

审查问题由控制器分配稳定 ID，原始定义及历次回应保留。生成者的可选
`issue_responses` 只能说明修订或提出带来源的争议，不能关闭问题；
后续独立审查必须逐个回应旧的未关闭问题，不能改 ID、降级或悄改闭合条件。
每次候选变更都重新检查五类要求和全部必要事实，清空该轮角色的旧原文访问记录。
候选历史和审查历史分别保存版本、摘要与内容，最终发布只使用最终版本的审查。

最多两轮修订；仍存在重大问题或未通过检查则 `needs_review`，保留候选供查看，
没有人工一键跳过发布。仅排版措辞类 minor 可随报告保留。
新模板位于 `backend/config/report_harness/`，创建 run 时冻结，旧模板不受影响；
没有报告字数目标或最低段落数。合成修订测试不证明真实审查能发现语义错误。

## 物理请求与预算

新链路的 `TransportRoles` 使用固定角色配置和 `BudgetedTransport`，不复用旧 provider
的隐藏重试、端点回退或模型驻留操作。每次 HTTP attempt 先在 PostgreSQL 登记 intent，
发送前原子转为 dispatched；本地发送线性化点之后视为在途，不承诺远端恰好一次。
请求与 run 共用行锁串行检查 fencing 和余额；取消不能被迟到的结果覆盖。

已知 usage 结算后释放未用预留；缺失或无效 usage 保留全部 token 预留并暂停，
超出预留如实计账且停止后续请求。预算视图从账本聚合，迟到结算不修改运行状态、
正文或状态版本。preparing 必须容纳生成与最终独立审查；其他动作保留最终审查容量。
活动时间使用数据库时钟累计，暂停期间不增加。

token 上界必须由服务端经过验证的协议证明提供，包含可计费输入和输出；
当前合成协议证明只适用于测试，不能证明真实模型的 token 上界。
没有经验证价格表时硬货币预算拒绝启动。真实端点、模型、资料与费用仍需独立批准。

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

当前运行迁移版本序列为 1、2；版本不匹配拒绝使用新模式，不能降级成忽略持久化的执行。
版本2解除会话到run/补证的删除外键，保留run到账本与事件的RESTRICT约束，
并增加 `session_deletion_barriers`，不能只删除会话而遗留可执行run。

取消在数据库事务内使旧令牌失效、拒绝未发送intent并将dispatched记为未知。
执行任务每250毫秒观察持久屏障，租约心跳仍每5秒更新；
本地关闭不代表能撤销已发送的远端调用。控制SSE断连会持久取消，
清理区屏蔽重复异步取消并等待worker关闭，不能断连后后台继续。

会话删除先在同一事务内建立屏障、撤销run、保留账本，再删除会话行；
事务提交后再清理原有会话文件。读写和历史文件迁移均检查屏障，防止迟到写回复活。
文件清理仅限解析并核验后的当前会话目录及其后代；客户端元数据不能扩大删除范围。
Windows 会话锁使用大小写等价键；屏障同时阻断大小写别名，拒绝复用其他会话目录的ID。
目录组件拒绝尾随点/空格、备用数据流、保留设备名等特殊路径。普通大小写ID保持可用。
这些数据库屏障保证要求迁移版本2；旧模式缺表兼容不具备同等持久删除保证。
无法证明归属的历史外部产物保留，不将会话删除宣传成对所有外部引用的物理清除。
已发布的终态正文不会被删除操作改写，但所有公开读取立即失去权限。
只有匹配原owner/run/request/attempt/token的内部结算可跨越删除屏障，
不能新增请求或恢复正文/发布权限；此结算入口不暴露为API或模型工具。

## 崩溃恢复

服务在新模式启用后每5秒有界扫描过期租约，锁内复核后标记
`suspended/orphaned_process`，不迁移表、不调用模型、不自动继续执行。
普通GET也会本地对账；恢复必须显式提交当前状态版本。
已经取消或发布的终态不可恢复，数据库不可用时清理器保守失败并记录无敏感诊断。

模型回合、准备步骤和工具结果保存于带版本及摘要的执行日志；完整结果直接复用，
工具原文读取状态一起恢复，问题ID及候选/审查版本保持稳定。
已结算但尚未写入步骤日志的完整响应可按请求摘要重放，不能重复发送。
未完成模型intent仅在新恢复令牌授权下重试，不复用失配的输入、策略、模型或工具契约。

未知请求默认返回409，只有显式接受重复计费风险且预算充足才允许重试；
旧未知预留继续占用，新请求另行预留，不重置同run预算。发布事务冻结预算快照及
最终事件，后续迟到usage只影响实时账本，不改写历史发布证据。
`backend/tests/test_run_recovery.py`使用真实子进程kill/restart与回环合成HTTP；
该证据不证明真实provider语义、实际费用或报告质量。

## 界面与导出

旧报告模式仍为默认；证据报告模式独立查询会话证据与分页运行列表，
不把候选或待审结果写入旧会话的成功报告字段。刷新只读状态，不恢复执行。
证据版本冲突保留本地编辑并返回409；未知请求恢复必须显式确认风险。
切换会话或离开页面会关闭控制流，按取消语义处理。

`GET /api/v1/report-runs`以会话、limit、cursor读取运行列表；candidate是所有者
专用详情入口，不授予发布或下载权限。导出入口为
`/api/v1/report-runs/{run_id}/exports/{format}`，支持md/docx/pdf。
未发布一律拒绝；正式模式每次核对历史批准，撤销立即返回409。
`mode=engineering`必须显式选择，文件名和正文强制带工程标记，
Word页眉与PDF每一页也保留标记。渲染使用独立临时目录，结束自动清理。

生产批准表只从`backend/config/report_harness/approved_release_bindings.json`读取，
初始为空，不提供API写入口。加载检查固定路径、schema、重复绑定、
当前进程身份的写权限及父目录替换权限；不会自动改ACL。
普通可写Git工作区不能作为生产批准源，需在获准部署时另行设置只读运行身份。
批准引用的本地JSON验收证据位于同目录`approved_evaluations/{摘要}.json`，
其内容SHA-256和只读权限也必须匹配。

新运行只有在非合成模式、构建清单和四摘要批准绑定都有效时才能冻结质量资格。
可选`build_manifest.json`含`version=1`、`clean=true`、`source_hashes`和`code_digest`；
源码清单覆盖后端app、前端src、报告角色模板及依赖清单，运行时重算，
缺失或不一致不赋予正式资格。服务端不生成构建清单或验收证据。
历史运行仍按其冻结绑定核验，不因新代码版本自动改写历史质量状态。
测试只注入只读历史fixture和注册表；合成创建路径始终为engineering_only。

`frontend/tests/harness-e2e.mjs`通过真实浏览器、回环HTTP及独立PostgreSQL验证
保存刷新、证据冲突、取消、未知请求恢复、历史批准撤销及桌面/移动端布局。
生成与审查使用合成角色，不证明实际模型质量或物理计费；相关物理请求预算另由
后端回环transport测试覆盖。正式下载正分支仅用测试注入的历史批准fixture，
下载仍强制工程标记，绝不写入真实批准表。

浏览器入口只用`REPORT_HARNESS_TEST_DSN`，不读取业务`.env`。
API默认端口18081，若被Windows保留则显式设置`HARNESS_TEST_API_PORT=18281`；
前端固定15174，端口被占用立即失败，不接管已有服务。
`REPORT_HARNESS_E2E_OUTPUT`指定本次截图、公开事件和结果目录。
runner退出会关闭自己启动的服务并清理自己创建的数据库行。
真实质量门尚未获得模型、预算及材料授权；浏览器通过不等于完整E门或Q门通过。

## 离线工程门

在`D:/MCP_Server/TS_analysis_report`执行，DSN仅可指向回环专用测试库：

```powershell
$env:REPORT_HARNESS_TEST_DSN = 'host=127.0.0.1 port=15432 dbname=safetyraise_harness_test user=postgres'
$env:HARNESS_TEST_API_PORT = '18281'
$env:PYTHONPATH = 'backend'
.\.venv\Scripts\python.exe -m evals.report_harness.run --output-dir C:/tmp/internal/think/.mission/20260916_23-14-07-safetyraise-report-evidence/verification/e-next
```

输出目录必须不存在，不能覆盖历史失败证据。runner串行运行完整后端suite、
前端测试、真实子进程清理回归、TypeScript、构建和浏览器；
任何失败、skip或缺少必需浏览器场景都拒绝关闭E。
`manifest.json`列出故障矩阵与旧标签基线；`result.json`保存提交、工作区状态、
命令/退出码/耗时、源码/策略/迁移/构建配置及产物SHA-256。
验证过程中源码变化、旧标签变化或真实批准表变化也判失败。
公开事件通过纯本地`replay_events`重放，不导入服务或网络执行器。
`engineering_gate=passed`仅证明该指纹版本的工程门，`quality_gate`仍为`not_run`，
不写生产批准、不打最终标签、不推送或部署。
测试服务从初始化阶段注册独立清理回调，关闭连接池失败也会尝试删除自身测试行；
浏览器runner从启动时观察进程退出，信号退出不重复等待，强制清理有界。
隔离API需正常退出，否则工程门失败。
