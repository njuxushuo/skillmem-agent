# Stage 1：B0-compatible 测量协议

本阶段实现测量基础设施，**真实 DeepSeek 为 `not_run`**。本轮没有获批付费预算；不读取或使用聊天中曾出现的凭据。离线与 Mock 数字不代表真实模型效果。

## 版本与边界

- B0 固定上游：`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`，标签 `b0-upstream`；Stage 0：`stage-0`。
- 当前只提供 `B0-compatible`：相同的8工具、System Prompt、40条历史裁剪、循环上限、审批与内存 Store；追加 DeepSeek 适配与共同计量。
- `graph.py/tools.py/policy.py/store.py/offline_model.py` 不修改。原 `tools_used` 仍是提议序列。没有 Stage 2 的单调用限制、幂等或审批恢复修复。
- `B1/M/MS/MSP` 未实现；不能将本阶段结果包装成这些变体的消融实验。
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

新 `--tasks` 路径失败返回退出码1，成功返回0；旧场景入口与其退出码缺陷保持B0不变。CI 使用禁网入口并运行全部测试，无真实 Key。

未来获得预算后，可在环境中设置 `DEEPSEEK_API_KEY` 并显式选择 `--provider deepseek`。该命令会产生费用；本阶段未运行它。程序不会自动加载 `.env`，不会自动运行 smoke。

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

2026-10-17 只读核对官方 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls) 、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) 和 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)：示例使用 `deepseek-flash` 与显式关闭 thinking。模型可用性和账户权限未验证。将来开启思考模式需重新解决 `reasoning_content` 回传，不能只改一个环境变量。

默认不重试便于计量；如果配置 SDK retries > 0，`model_calls` 是 **LangChain 逻辑调用数**，不等于每次 HTTP 尝试数；可能付费但未获得 usage 的请求仍 unknown。工具无自动重试逻辑；模型重新提议或审批重入照实记录。

## 任务输入与隔离

`EvalCase` 要求 `case_id/category/customer_id/fixture/clock/turns/approval_script/expected`。当前固定夹具 `store.json`、日期2025-10-01；每 task×repeat 新模型、新 Store、独立 UUID thread、新临时 SQLite。数据库在任务结束关闭并删除；事实快照留在本地轨迹。测试重复两次验证退款互不污染。

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

必要信息当前用固定业务字段/短语匹配；这是可复现的受限 Oracle，不能完整识别否定句、矛盾回答或所有同义表达。适用于这些明确的开发夹具，不能宣称通用语义评判。Stage 5 冻结任务前须扩展分层规则/审查误判。

`evaluation.sop_checks` 当前仅测软 SOP **退款提议前，同订单存在成功资格查询**。适用分母为有退款提议的任务，单任务内每次退款提议均计检查；不适用为null。它与 Task Success 分开：B0允许直接合法退款，业务成功但 SOP=false。此指标不等于完整业务安全或未来四 Skill SOP 的合规率。

## Trace 与计量

`TraceRecorder` 为单任务、同步 LangChain callbacks；`ContextVar` 关联并行工具 invocation ID，锁只保护日志，不串行化业务。`SupportService` 可选注入；默认 runtime 行为不变。完整任务生命周期由 runner 观测，因此直接使用 Service callbacks 只得到模型/工具层事件。

- `model.request/response/error`：消息、有效工具schema/请求参数、调用ID、耗时、原始与规范化usage，来源 offline/mock/provider。
- `tool.proposed`：含有效与非法JSON提议；`tool.attempt`：进入BaseTool；`tool.execution_start`：通过参数验证后真正进入函数。
- `tool.execution_end`：成功/业务拒绝；`tool.execution_error`：函数异常；`tool.paused`：GraphInterrupt原样抛回，非普通失败。
- `tool.observation`：ToolMessage，包括不存在工具/参数验证失败；按tool_call_id去重避免图回调重复统计。重复使用同一call_id的异常模型不适用于此去重口径，执行事件仍逐次保留。
- `graph.interrupt/resume/error`、`action.user/result`、`task.state/end`：审批决定、状态前后快照与差异、终止原因。含run/thread/step，active_skill=null。

计数定义：proposed为模型提议数，executed为真实函数入口数，success为非拒绝正常返回数，rejected为业务拒绝，execution_error为函数异常，protocol_error为错误ToolMessage，paused为审批暂停，reentry为重复call_id的函数入口。各错误维度可能重叠，不可简单相加。审批恢复进入相同函数是实际工作，应计入 executed/reentry；不等于新增退款。退款成功以 Store 为准。

Usage 优先取 `AIMessage.usage_metadata`，保留 `response_metadata.token_usage`；原始字段缺失时覆盖SDK的补零，输出null。每个字段仅在所有逻辑请求都获得该字段时汇总总量，否则null；`known_*`是已知小计、`unknown_*_calls`为缺失数。0只有服务端明确返回该字段0时有效。缓存hit/miss保留原始字段，**不额外加到prompt_tokens**。成本暂为null，未引入未经验证的价格。

结果输出 `summary.json/results.jsonl/results.csv`；统计包含所有任务，失败不删除。Token总数若任一任务unknown则为null。offline与mock的成功率仅验证软件协议，不是 DeepSeek 效果。

## 输出与安全边界

默认 `.local/runs/<run_id>/`，仓库内原始输出只允许`.local/`（解析symlink后校验），外部临时目录可用于pytest。轨迹文件0600、新建运行目录0700；拒绝覆盖已有轨迹。已有父目录权限由环境负责。

Key、authorization等字段和常见凭据格式脱敏，runner显式注册当前模型密钥以清除错误消息中的精确值；不记录请求头。**这不是通用PII匿名化工具**：完整对话、业务快照仍只能留在Git忽略的本地目录。GitHub仅提交人工审查的合成夹具摘要。

日志写入失败不改变业务结果或重放工具，runner将该测量标记error。无法建立输出目录等基础设施错误直接失败。业务Store仍内存、审批先领取后resume、同thread并发和多interrupt等风险属于Stage 2，均未提前修复。
