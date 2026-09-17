# 报告 Harness

## 当前边界

本模块是报告证据与独立审查运行控制的开发实现，旧报告链路保持默认。
生产工厂关闭新执行器，不能通过客户端字段选用合成角色或测试批准表。
合成角色只用于控制流测试，不能证明真实报告质量。

当前首条切片只开放无补证、无知识检索的合成成功路径；其他能力未完成前明确拒绝。
候选、审查与发布正文存入专用 PostgreSQL 表，不写旧报告输出目录。
真实质量验收、模型外发、生产迁移和部署均不是运行测试的附带操作。

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
