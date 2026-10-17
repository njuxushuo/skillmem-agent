# B0-compatible / B1 测量协议

Stage 1 首次交付的真实 DeepSeek 状态为 `not_run`，历史报告保留不改。2026-10-17获批一次在线补充验收，8次真实请求成功；原Oracle因日期/币种表示判0/3，通用格式修正后对原轨迹离线复判3/3，未追加API调用。见[在线补充报告](reports/phase_1_live_smoke.md)。离线与Mock数字仍不代表真实模型效果。

## 版本与边界

- B0 固定上游：`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`，标签 `b0-upstream`；Stage 0：`stage-0`。
- B0权威锚点`b0-compatible`=`0021b1d96a942bbd4259317bcfd5f494c33d7617`；当前兼容模式保留原8工具/System/历史/内存业务与原控制行为。B1默认启用本阶段可靠执行与SQLite业务存储。
- Stage 2修改graph/tools/store/service以实现B1；policy/offline_model仍未修改。原`tools_used`仍为提议序列，实际执行见Trace。
- B1已实现；M/MS/MSP尚未实现。本阶段只有可靠性/兼容性测试，没有架构优化消融结论。
- `data/eval_tasks.json` 是 **7个 Stage 1 开发夹具**，不是 Stage 5 的 Frozen Final。原11场景文件不变。

## 运行

```bash
# 禁网并清除 API Key；全测试、原场景、新任务分别执行
.venv/bin/python scripts/run_offline.py pytest -q
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks

# 可配置离线重复测量（仍明确使用规则模型）
.venv/bin/python -m support_agent.evaluate --provider offline \
  --tasks data/eval_tasks.json --repeat 2 --output .local/runs
```

新 `--tasks` 路径失败返回退出码1，成功返回0；旧场景入口本阶段也修复为失败退出1。CI 使用禁网入口并运行全部测试，无真实 Key。

通用 `--provider deepseek` 入口会产生费用，本身不提供本次的美元预算保证。本次真实验收使用 `scripts/run_live_smoke.py` 的独立受限HTTP transport：15次/2美元双上限，已完成且持久化账本拒绝重复执行。仅该脚本支持显式 `--env-file`；程序不会自动加载 `.env` 或自动运行付费 smoke。

## DeepSeek 配置与兼容约定

`models.get_model('deepseek')` 使用现有 `langchain_openai.chat_models.base.BaseChatOpenAI`（ChatOpenAI通用父类），无需新依赖：

| 环境变量 | 默认值 |
|---|---|
| `DEEPSEEK_API_KEY` | 无，必须由环境注入 |
| `DEEPSEEK_MODEL` | `deepseek-flash` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_TIMEOUT` | 60秒 |
| `DEEPSEEK_MAX_TOKENS` | 1024，输出上限 |
| `DEEPSEEK_TEMPERATURE` | 0 |
| `DEEPSEEK_MAX_RETRIES` | 0 |

固定 `extra_body.thinking.type=disabled`、非流式、Chat Completions（`use_responses_api=False`），不加 strict schema/parallel_tool_calls 参数。`bind_tools` 转换工具 schema，`ToolNode` 保留 `tool_call_id`。当前库的ChatOpenAI子类会将max_tokens编码为OpenAI专属的max_completion_tokens；DeepSeek分支使用BaseChatOpenAI保留官方max_tokens字段，Mock严格断言不存在max_completion_tokens。依赖精确锁定；升级时必须重测工具绑定与请求字段。

2026-10-17 只读核对官方 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls) 、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) 和 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)：示例使用 `deepseek-flash` 与显式关闭 thinking。本次已用该模型完成真实冒烟；可用性只代表本次账户与时间。将来开启思考模式需重新解决 `reasoning_content` 回传，不能只改一个环境变量。

B1要求SDK retries=0，防止绕过显式请求次数预算。B0如果配置 SDK retries > 0，`model_calls` 是 **LangChain 逻辑调用数**，不等于每次 HTTP 尝试数；可能付费但未获得 usage 的请求仍 unknown。工具无自动重试逻辑；模型重新提议或审批重入照实记录。

## 任务输入与隔离

`EvalCase` 要求 `case_id/category/customer_id/fixture/clock/turns/approval_script/expected`。当前固定夹具 `store.json`、日期2025-10-01；每 task×repeat 新模型、新 Store、独立 UUID thread、新临时 SQLite。B1将业务快照、幂等回执、审批journal与执行计数也存入该临时数据库。数据库在任务结束关闭并删除；事实快照留在本地轨迹。测试重复两次验证退款互不污染。

用户 turn 按顺序调用 `SupportService.chat`；等待审批时按脚本调用 `resolve_approval`。无可用审批脚本则停止该任务并标记 incomplete，多余未消费审批同样失败。任务异常记录错误类型和本地详情，继续后续任务。评测器不替 Agent 选择工具，不替业务执行退款。

每条结果带源码 SHA、dirty 状态、应用源码内容哈希、模型配置哈希、任务集哈希、业务夹具与政策哈希。`dataset_hash` 为 JSON 内容规范化后 SHA-256；文件的原始字节 SHA-256 可以不同。模型元数据采用字段允许列表，去掉 URL 用户名/密码/query，不序列化客户端或模型对象。

## Oracle 与 SOP

`evaluation.judge` 比较完整前后 orders/refunds/tickets，只有预期业务变化被允许；同时要求对话完成和必要回复事实。成功判定不要求唯一工具序列。

| 家族 | 正例 | 失败例 |
|---|---|---|
| 订单/物流 query | 1002的tracking/eta、1005的item/total/currency/status在回复中，订单号正确，无副作用 | 缺少事实、无订单号、其他订单被取消、只说done |
| 退款 refund | 恰好新增一笔、订单/金额/reviewer正确，order.refunded同步，其他状态不变 | 只口头宣布完成、金额/订单/reviewer错误、额外副作用、待审批未完成 |
| 退款审批 | 1001退款89.00，脚本实际消费了同订单同金额、相同reviewer的批准 | 未批准、错误金额或订单、未消费审批、拒绝后仍产生退款 |
| 取消/正确拒绝 cancel/refusal | 1004被取消且无额外变更；1002被拒且正确说明已发货、状态不变 | 未取消却说完成、拒绝时退款、理由缺失、其他状态变化 |

必要信息当前用固定业务字段/短语匹配，`reply_fact_present`额外识别EUR/€和有效英文日期与ISO日期的等价形式，错误币种/日期仍拒绝；这是可复现的受限 Oracle，不能完整识别否定句、矛盾回答或所有同义表达。适用于这些明确的开发夹具，不能宣称通用语义评判。Stage 5 冻结任务前须扩展分层规则/审查误判。

`evaluation.sop_checks` 当前仅测软 SOP **退款提议前，同订单存在成功资格查询**。适用分母为有退款提议的任务，单任务内每次退款提议均计检查；不适用为null。非JSON/非对象资格结果不崩溃、不视作合格，只有`eligible is True`可作为证据，异常结果另计`sop_invalid_result_count`。它与 Task Success 分开：B0允许直接合法退款，业务成功但 SOP=false。此指标不等于完整业务安全或未来四 Skill SOP 的合规率。

## Trace 与计量

`TraceRecorder` 为单任务、同步 LangChain callbacks；`ContextVar` 关联并行工具 invocation ID，锁只保护日志，不串行化业务。`SupportService` 可选注入；默认 runtime 行为不变。完整任务生命周期由 runner 观测，因此直接使用 Service callbacks 只得到模型/工具层事件。

- `model.request/response/error`：消息、有效工具schema/请求参数、调用ID、耗时、原始与规范化usage，来源 offline/mock/provider。
- `tool.proposed`：含有效与非法JSON提议；`tool.attempt`：进入BaseTool；`tool.execution_start`：通过参数验证后真正进入函数。
- `tool.execution_end`：成功/业务拒绝；`tool.execution_error`：函数异常；`tool.paused`：GraphInterrupt原样抛回，非普通失败。
- `tool.observation`：ToolMessage，包括不存在工具/参数验证失败；按ToolMessage.id去重避免图回调重复统计；在首次观测时为无id消息分配身份。重复tool_call_id产生的新结果仍记录，工具协议ID不变。
- `graph.interrupt/resume/error`、`action.user/result`、`task.state/end`：审批决定、状态前后快照与差异、终止原因。含run/thread/step，active_skill=null。

计数定义：proposed为模型提议数，executed为真实函数入口数，success为非拒绝正常返回数，rejected为业务拒绝，execution_error为函数异常，protocol_error为错误ToolMessage，paused为审批暂停，reentry为重复call_id的函数入口。各错误维度可能重叠，不可简单相加。审批恢复进入相同函数是实际工作，应计入 executed/reentry；不等于新增退款。退款成功以 Store 为准。

Usage 优先取 `AIMessage.usage_metadata`，保留 `response_metadata.token_usage`；原始字段缺失时覆盖SDK的补零，输出null。每个字段仅在所有逻辑请求都获得该字段时汇总总量，否则null；`known_*`是已知小计、`unknown_*_calls`为缺失数。0只有服务端明确返回该字段0时有效。缓存hit/miss保留原始字段，**不额外加到prompt_tokens**。通用runner成本仍为null；受限smoke账本单独按已核对价格给费用上界和缓存感知计价，实际账单未知仍null。

结果输出 `summary.json/results.jsonl/results.csv`；统计包含所有任务，失败不删除。Token总数若任一任务unknown则为null。offline与mock的成功率仅验证软件协议，不是 DeepSeek 效果。

## 输出与安全边界

默认 `.local/runs/<run_id>/`，仓库内原始输出只允许`.local/`（解析symlink后校验），外部临时目录可用于pytest。轨迹文件0600、新建运行目录0700；拒绝覆盖已有轨迹。已有父目录权限由环境负责。

Key、authorization等字段和常见凭据格式脱敏，runner显式注册当前模型密钥以清除错误消息中的精确值；不记录请求头。**这不是通用PII匿名化工具**：完整对话、业务快照仍只能留在Git忽略的本地目录。GitHub仅提交人工审查的合成夹具摘要。

日志写入失败不改变业务结果或重放工具，runner将该测量标记error。无法建立输出目录等基础设施错误直接失败。以上内存Store/领取后删除pending/并发风险仅保留在B0；B1执行可靠性及边界见下文。

真实API状态由`deepseek_api_status`判定：无调用not_run、尝试后无成功响应failed、部分响应/错误partial_failure、全部收到响应api_succeeded。API成功与Task Success独立；失败分类区分api_error、budget_limit、model_decision_failure、oracle_failure、setup_error与measurement_error。Mock/offline不得标成真实成功。


## Stage 2：B1统一安全基线

实现提交、Gate和证据见[phase_2](reports/phase_2.md)。选择`run_cases(..., variant="B1", limits=ExecutionLimits(...))`，或CLI `--variant B1`；未指定时读取`SKILLMEM_VARIANT`，默认B1。每条结果保存variant和execution_limits，task.start同样记录。B0没有这套安全预算，字段为null；核心优化归因只比较共用该预算的B1/M/MS，不以B0的较弱安全语义冒充优化收益。

默认每用户轮次8模型、8工具尝试；审批重入计入原工具预算，重启不重置。每次chat/resume有120秒截止时间，工具5秒协作截止。工具参数错误/多调用可交给模型改正，但仍消耗模型预算；函数异常直接终止，不自动重试副作用。模型SDK retries必须0；DB预算是尝试预留，Tracing函数入口才是实际执行数，因崩溃等原因两者不一定相等。

新增`tool_idempotent_replay_count`、`guard_rejected_count`；保留提议/入口/正常返回/业务拒绝/异常/暂停/重入指标。`execution.stopped`给出模型或工具预算耗尽、模型异常、工具超时/失败、协议ID不可恢复等原因。业务幂等回放仍是一次实际函数调用，另标记回放次数，不能当成新的退款。

工具错误输出JSON含error/status/retryable。retryable表示临时性质，**不是自动重试指令**；如业务成功而checkpoint失败，仅从原checkpoint显式恢复，稳定操作回执阻止二次副作用。本阶段93.33%语句覆盖率、153pytest、B0/B1各11/7场景/任务均为离线软件证据；本阶段真实API为not_run、Token和费用未知，不填0。

Stage 3/4引入优化时须继续使用同一ExecutionLimits、工具/审批实现、固定模型/数据/日期和usage口径；若启用不同预算必须另列实验组，不能直接比较原组的成本和成功率。B1使用全量8工具与原历史，尚未实现Context Token Budget。

## Stage 3 配置与诊断

新增M变体复用B1安全层。`run_cases`可传`context_strategy/context_budget`，结果与task.start保存有效设置。`context.built`记录估算及分项，`context.measured`按同次模型call_id关联actual_input_tokens与usage_source；Mock计量与真实provider严格分开。B1 context字段null，完整checkpoint不裁剪。G3阶段仅离线/Mock验证，不代表M真实任务效果。

## Stage 4路由计量与SOP公平性

MS=M+四Skill；full_sop只允许B1/M，全文与MS单独加载的四份SOP相同。每任务保存full_sop和skill_sop_hash。tool_proposed_count包含控制提议，business_tool_proposed_count剔除select_skill；tool_executed_count仍为业务函数入口，skill_routing_count/control_executed_count为通过Guard后选择逻辑入口（包括未知ID的拒绝），total_tool_executed_count是两者之和。skill_switch_count仅实际改变active_skill的成功选择，重复选择仍计成本。skill_model_calls是响应含select_skill的模型调用数，skill_*tokens为其usage子集，所有模型调用已计入input/output/total，不应再加一次。skill_routing_latency_ms仅控制函数耗时，完整路由模型耗时已在model.response与总任务latency内。
