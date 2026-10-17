# Phase 1 Report — SkillMem Agent V1

日期：2026-10-17（Asia/Shanghai）。范围：**仅 Stage 1：DeepSeek 适配与 Agent Evaluation 基础设施**。

## 1. 目标与实际完成

**G1：PASS（离线/Mock Gate）；真实 DeepSeek：`not_run`。** 本阶段未调用付费 API，未测试聊天中提供过的 Key。原因是本轮没有明确批准真实调用预算；`not_run`不是“认证失败”或“调用成功”。只有未来真实 smoke 通过才能声称端到端 DeepSeek 集成已验证。

已实现：

- DeepSeek 非思考 Provider：环境配置、标准 Chat Completions、全量原8工具绑定与多轮 ToolMessage；Mock验证请求字段、工具schema、ID配对、审批resume、usage、401/429/500/超时。
- 可选 TraceRecorder：模型输入/响应/耗时/usage，工具提议、参数验证后的真实执行、业务拒绝/函数异常/审批暂停，ToolMessage关联。Runner增加任务生命周期、interrupt/resume、业务快照与差异、终止原因。
- 独立任务评测器：多轮用户脚本、审批脚本；每case×repeat独立模型、Store、临时SQLite、thread；三类业务Oracle；失败继续、结构化JSONL/CSV/JSON及非零失败退出码。
- B0-compatible测量路径、7个开发夹具、34个新增pytest、公开脱敏统计和一条包含所有30个事件步骤的审批轨迹投影。
- 原24个pytest、原11场景全绿；精确依赖不变；Git提交前凭据模式/忽略规则/架构审计通过。

未实现：真实模型成功率/费用、B1可靠性修复、Skill、TaskMemory、Context压缩/预算、Planning、完整SOP指标、Stage 5 Frozen Final/消融实验。没有创建这些阶段的空模块。

## 2. Git 与源码

| 项目 | 值 |
|---|---|
| 目标GitHub仓库 | https://github.com/njuxushuo/skillmem-agent |
| Branch | `main` |
| 固定上游 / B0 | `b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`，`b0-upstream` |
| Stage 0交付 | `28823a645b4eaa7113831a74c47f39571bc3664a`，`stage-0` |
| Stage 1代码提交 | `487d533fea6148e70cc1fd8d2ec2230f69097915` |
| 代码提交标题 | `feat(stage1): add DeepSeek adapter and observable baseline evaluation` |
| 最终交付提交 | 本报告与两个脱敏artifact随后独立提交；用`git rev-parse 'stage-1^{commit}'`获取 |
| 推送状态 | **not_pushed**：HTTPS预检缺少GitHub认证，未伪造远端commit链接 |
| 远端CI | **not_run**；本地运行相同禁网检查通过 |

报告不嵌入自身Git SHA，避免自引用；最终回复提供完整交付SHA。代码提交后的7任务和单个Mock取证运行均记录该代码SHA、`working_tree_dirty=false`。完整58测试在相同代码内容上提交前运行，源码内容哈希一致。

推送预检：

```bash
GIT_TERMINAL_PROMPT=0 env -u https_proxy \
  git -c http.proxy=http://127.0.0.1:36123 push --dry-run origin main
```

真实退出128：`fatal: could not read Username for 'https://github.com': terminal prompts disabled`。没有force push。目标地址不缺失；缺的是此运行环境下配置给Git的有效写入认证。配置认证后可推送：

```bash
git push origin main
git push origin refs/tags/stage-1
```

### 变更文件与来源

所有新增实现由本阶段编写；上游业务和参考Java架构均未重新移植。

| 文件 | 变化与原因 |
|---|---|
| `src/support_agent/models.py` | `get_model()`新增deepseek分支，读取Key/model/base_url/timeout/max_tokens/temperature/retries；验证配置；保留其他provider。 |
| `src/support_agent/service.py` | **仅新增8行**：可选trace参数、包装原工具函数、`_run`注入callbacks。原审批/会话/图控制逻辑不变。 |
| `src/support_agent/tracing.py` | 新增276行；callbacks、真实工具入口计数、usage汇总、本地写入与脱敏。 |
| `src/support_agent/evaluation.py` | 新增369行；任务schema、业务Oracle、隔离runner、SOP与元数据/结果输出。 |
| `src/support_agent/evaluate.py` | 增加`--tasks/--output/--repeat`，委托新runner；原`run_scenarios`及默认场景行为保持。 |
| `tests/test_deepseek.py` | 新增260行/15个参数化后测试；真实SDK+MockTransport，无网络。 |
| `tests/test_evaluation.py` | 新增250行/19个参数化后测试；业务Oracle、trace、失败隔离、审批、计数及B0行为。 |
| `data/eval_tasks.json` | 7个Stage 1开发任务，固定C100与日期2025-10-01，明确预期状态与审批脚本。 |
| `.env.example` | 空Key占位和实际生效DeepSeek设置；默认offline；没有未来SKILLMEM开关实现。 |
| `scripts/run_offline.py` | 增加固定offline的`tasks`入口；继续清除Key/禁socket/关闭外部tracing。 |
| `.github/workflows/ci.yml` | 原回归运行所有新增测试，增加7任务禁网检查；依赖锁不变。 |
| `README.md` | 更新当前实现/未验证状态、实际目录、运行入口和来源说明。 |
| `docs/evaluation_protocol.md` | 当前测量契约、字段、计数、Oracle局限及复现方式。 |
| `docs/reports/phase_1.md` | 本报告、Gate和架构审计。 |
| `artifacts/stage_1_summary.json` | 脱敏测试摘要、7任务结果、Mock usage示例。 |
| `artifacts/stage_1_trace_sample.json` | 30步合成夹具审批轨迹投影；隐藏完整系统/历史消息及无关订单，ID统一别名。 |

应用从12文件/1026行变为14文件/1718行，**净增692物理行**。测试由4文件/277行变为6文件/787行，新增510行。Ruff共检查21个Python文件（14应用＋6测试＋1验证脚本）。无新第三方依赖或数据库栈。

## 3. 核心实现、调用链与兼容性

### 模型适配

`models.py:get_model`（第17行）：DeepSeek使用`langchain_openai.chat_models.base.BaseChatOpenAI`，为现有ChatOpenAI的通用父类。已核对官方 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)、[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion)。

当前锁定库的ChatOpenAI子类会将`max_tokens`改写为OpenAI的`max_completion_tokens`，而DeepSeek文档定义`max_tokens`。因此使用通用基类保留正确请求字段，Mock严格断言`max_tokens=123`且不存在`max_completion_tokens`。这一点是依赖源码和Mock验证后的最小兼容选择，不是通过只替换模型名宣称兼容。

默认值：model=`deepseek-flash`、URL=`https://api.deepseek.com`、timeout=60秒、输出1024、temperature=0、retries=0。固定非流式、Chat Completions、`thinking.type=disabled`。不启用严格JSON schema、Responses API或thinking reasoning_content。模型名可配置；真实服务端/账户可用性尚未验证。

### 新模块接口与依赖

| 模块/核心函数 | 输入→输出及职责 |
|---|---|
| `tracing.py:private_directory` | Path→受限本地输出目录；仓库内只允许`.local`，支持pytest外部临时目录。 |
| `tracing.py:usage_of` | AIMessage→规范字段与原始usage；缺失为null，防止SDK补零误计。 |
| `TraceRecorder.instrument_tools` | 原StructuredTool列表→保持schema的函数包装副本；验证通过后才记录execution_start，原结果/异常原样返回/抛出。 |
| `TraceRecorder` callbacks/`emit` | LangChain事件→本地JSONL；含run/thread/step/调用ID/耗时/来源。 |
| `TraceRecorder.statistics` | 事件→提议/执行/成功/拒绝/异常/重入与Token汇总。 |
| `evaluation.py:EvalCase` | JSON→校验后的多轮任务、审批和预期schema。 |
| `evaluation.py:snapshot` / `judge` | Store事实→不可变快照；前后状态/回复/审批证据→成功与失败原因。 |
| `evaluation.py:sop_checks` | Trace→退款前资格查询这一条软SOP的适用次数/通过次数。 |
| `evaluation.py:run_cases` | 任务列表/output/provider/factory/repeat→全部任务summary；隔离、异常继续、输出文件。 |
| `evaluation.py:provenance/model_metadata` | 固定源码/数据/有效配置→可核查的SHA与配置哈希，不序列化Key或客户端。 |

依赖为`evaluate → evaluation → SupportService → 原graph/ToolNode`；`evaluation/service → tracing → LangChain callbacks/消息类型/标准库`。Tracing不反向依赖service/evaluation，没有循环依赖。评测器不直接伪造工具副作用。

调用链：

```text
新评测CLI → run_cases → 新Store/SQLite/thread/model/TraceRecorder
  → SupportService.chat → 原StateGraph(agent ↔ ToolNode)
  → 模型callbacks记录输入/输出/提议
  → 原BaseTool参数验证 → 函数包装记录执行开始 → 原业务工具/Policy
  → ToolMessage（含原tool_call_id）→ 下一次模型调用
  → 大额退款原interrupt → runner记录/消费审批脚本
  → SupportService.resolve_approval(Command resume) → 原工具重入
  → 最终快照/Oracle/SOP/计量 → 本地结构化结果
```

**兼容性依据：** `graph.py`完全不变，仍MessagesState与原节点/边；Service不改变控制分支。审批暂停原样抛GraphInterrupt，不作为普通错误吞掉；resume仍用同一call_id，函数重入单独记录。原单轮多工具执行保持（Mock验证两个工具均执行），不提前加入Stage 2单调用限制。原直接合法退款无需先显式资格工具的行为保留，业务成功但软SOP=false。

实际执行为何需要函数包装：LangChain `on_tool_start`发生在参数校验之前，仅计callback会把校验失败算成执行。因此仅观察原函数入口，不修改ToolNode或工具业务代码。`ContextVar`关联并行调用，锁仅保护日志；不引入业务串行化。

## 4. 验证：命令与真实结果

Python3.12.9/Linux x86_64；62项依赖锁仍为Stage 0版本。主要依赖langgraph1.2.16、langchain-core1.6.12、langchain-openai1.7.2、openai3.33.0、pytest9.1.1、ruff0.17.1。`requirements.lock`、`pyproject.toml`均无变动，`pip check`通过。

```bash
COVERAGE_FILE=.local/stage1/.coverage .venv/bin/python scripts/run_offline.py pytest -q \
  --basetemp=.local/stage1/tmp \
  --cov=support_agent --cov-report=term-missing \
  --cov-report=json:.local/stage1/coverage.json
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

| 验证 | 真实结果 |
|---|---|
| 全部pytest | **58 passed in 6.13s**，0失败、0跳过 |
| 原pytest | 原4个文件逐字节未改；其中24项包含于58项，首次独立回归也24 passed |
| 新pytest | **34项**：DeepSeek15、Evaluation19 |
| 原离线客服场景 | **11/11**；原场景JSON未改 |
| 新开发任务CLI | **7/7**；代码提交后干净工作树运行 |
| 隔离重复运行 | `test_runner_isolation_multiturn_approvals_outputs`中7任务×2次=14/14，14个独立thread，无残留SQLite |
| 语句覆盖率 | **874/945 = 92.48677248677248%**；未声称分支覆盖率 |
| Ruff lint / format | 全通过；21 files already formatted |
| 依赖检查 | No broken requirements found |
| 真实DeepSeek smoke | **not_run**；API调用0，不用Mock成功替代 |

原24测试中的1项本来就执行11场景，新增runner集成也在pytest中，不能把这些再相加声称更多独立pytest。

### 关键边界证据

- `test_mock_multistep_tool_ids_and_usage`：真实SDK和模型适配栈，资格→退款→最终回复3次HTTP Mock请求；8工具schema中无客户身份参数；每个ToolMessage ID配对，Usage/缓存汇总正确。
- `test_mock_approval_resume_retains_pending_call_id`：中断/审批恢复后原call_id进入下一次模型请求；2提议、3执行开始、1重入。
- `test_missing_partial_zero_usage`、`test_mixed_known_unknown_usage_keeps_subtotal`：缺失、部分缺失、显式0和混合用量正确；无usage调用不补0。
- `test_http_errors_no_retry_or_fake_usage`、`test_timeout_recorded`：401/429/500/timeout，无默认重试、无假用量。
- `test_malformed_arguments_observed_without_execution`、`test_proposal_validation_and_business_rejection`：非法JSON、未知工具、缺参数、业务拒绝分别记录；不能把提议/验证尝试当执行。
- `test_oracle_positive_and_corrupt_outcomes`、`test_refund_approval_oracle_requires_matching_evidence`：正负状态、错金额/订单/reviewer/审批、额外副作用、缺事实与未完成，全部有断言。
- `test_failure_continues_and_no_fake_completion`、`test_invalid_case_and_model_failure_continue`：空case/模型初始化失败/口头退款不产生状态变化，后续任务继续。
- `test_function_exception_recorded_and_remaining_case_runs`、`test_loop_limit_still_original_fallback`：函数异常计数、快照和原循环上限升级工单；未改变业务错误策略。
- `test_trace_secrets_paths_and_write_failure_do_not_replay`、`test_trace_sink_failure_invalidates_measurement`：Key脱敏/路径限制/权限；写入失败不重试退款，测量标error。
- `test_parallel_calls_preserve_b0_behavior`、`test_instrumented_and_uninstrumented_business_equivalence`：保留B0多调用、直接合法退款以及相同结果/Store。
- `test_new_cli_failure_exit`：新任务路径失败退出1；旧场景入口退出缺陷未提前修改。

### 开发中真实失败与修正

1. 最初新测试30项中1项失败：使用原offline模型注入工具异常，错误ToolMessage不是业务JSON，offline忽略它并继续提议，实际执行8次后触发循环上限，而测试错误预期1次。改用“调用一次后输出最终回复”的可控脚本验证异常计数；另用独立测试保留原循环上限行为。**没有修改offline模型或工具错误策略。** 随后30项通过，最终补充边界成为34项。
2. 首次兼容Mock允许max_tokens/max_completion_tokens任意一个，官方API复核发现这一断言过宽。改用通用BaseChatOpenAI并严格检查max_tokens；重新执行15个DeepSeek测试及全部58测试通过。
3. Ruff提示ContextVar默认可变字典，改为None；不改变事件计数与工具行为。

## 5. Tracing 示例：完整脱敏 Action–Observation

来源：代码提交`487d533…`，offline规则模型，合成夹具`refund_approved`；不是实际顾客或真实LLM。完整30步事件投影见`artifacts/stage_1_trace_sample.json`，本表合并仅用于阅读。ID统一替换，金额与状态保留。

| 步骤 | Action / Observation |
|---|---|
| 1–2 | 开始；ORDER-A初始refunded=0、refunds=[]；用户要求为损坏商品退款。 |
| 3–5 | 模型请求(system+human)；响应提议`check_refund_eligibility(order_id=ORDER-A)`，记录call ID。 |
| 6–10 | Tool attempt→真实函数入口→返回eligible=true、max_amount=89.00 EUR、needs_human_approval=true→配对ToolMessage。 |
| 11–13 | 模型收到资格结果；提议`request_refund(order_id=ORDER-A, amount=89)`。 |
| 14–17 | attempt→函数入口→GraphInterrupt，记paused；没有退款副作用。 |
| 18–20 | Service返回awaiting_approval；记录interrupt；脚本由REVIEWER-A批准，记录resume。 |
| 21–25 | 相同tool_call_id、不同invocation_id再次进入函数；返回refunded/89.00/EUR/approved_by=REVIEWER-A；真实新增1笔退款；配对ToolMessage。 |
| 26–28 | 模型看到退款结果，最终回复退款89.00 EUR及订单；Service answered。 |
| 29–30 | 对比状态：refunded从0→89.00，refunds新增1笔、无pending；Oracle=true，termination=completed。 |

该任务：模型3次；工具提议2次；真实函数入口3次（资格1＋退款暂停1＋恢复1）；成功返回2次；暂停1；重入1；实际退款只有1笔。全部模型usage为null，来源offline。时延为本机真实墙钟耗时，不是API时延。

原始完整轨迹只保存在忽略目录：

- `.local/runs/3aa88744ea7241b193929eae647e9676/1-3.jsonl`
- 同目录`summary.json/results.jsonl/results.csv`

`TraceRecorder`不改变Tool/Graph业务返回；Trace写入失败会使测量失败，不会重放业务动作。直接只注入Service callbacks时仅覆盖模型/工具层；任务/审批生命周期与Store前后差异由runner补齐。

## 6. Evaluation 示例与结果口径

代码提交后运行7个开发任务，全部offline：

| Case | 输入/预期业务事实 | 判定 | 提议/执行 |
|---|---|---|---|
| shipping_multi_turn | 先看1005，再查1002物流；最终回复TRK-88213与2025-10-03，Store不变 | PASS | 2 / 2 |
| order_details | 查看1005，回复商品/18.00/EUR/delivered和订单号，Store不变 | PASS | 1 / 1 |
| refund_small | 1005损坏退款18.00；恰好1笔，approved_by=policy | PASS | 2 / 2 |
| refund_approved | 1001退款89.00；脚本批准后恰好1笔，approved_by=fixture-reviewer | PASS | 2 / 3 |
| refund_denied | 同订单退款，脚本拒绝；无退款，回复拒绝原因 | PASS | 2 / 3 |
| cancel_processing | 1004从processing变cancelled，无额外副作用 | PASS | 1 / 1 |
| cancel_shipped_refused | 1002已发货，明确拒绝且状态不变 | PASS | 1 / 1 |

总提议11、执行13；适用“退款前资格查询”软SOP任务3/3通过。所有任务都有最终状态和失败原因字段；没有因失败丢弃结果的路径。其余错误例属于可控单测，不混入这7项开发任务成功率。

Oracle要求完整业务状态一致，只允许期望的退款或取消变化，独立于精确工具顺序。查询/拒绝的必要信息目前采用固定事实和短语匹配；不能识别所有否定、同义改写或自相矛盾回答，这是受限Oracle的重要边界。SOP只覆盖资格查询先行，不代表全量业务安全合规。

### 可追溯元数据

| 项目 | SHA-256 |
|---|---|
| 应用源码内容hash | `4a9590331a6e796739a8105d960ab539aa8d8bbf804621177ddf44df8cb13a15` |
| 规范化任务JSON hash | `f4ebb43adf785c0c130d8ee0a811346b863c838537fa0ebf8d226175d2a1fc7a` |
| store.json字节hash | `246f50b7ad509a1a96046676ca9fe643a40a63f11a1b2c8d20fdf725523147de` |
| policies.md字节hash | `a39d4544e6a5a1c8eb6da3c8ce1ed190d81e66f4e4509a8192781b3ac273f6bf` |
| offline模型配置hash | `f7ed32cf93378bcf3d37a8f6f3173012766dae0d78cf1e0a84e2eb5e049333e9` |

`source_hash`由所有应用.py内容排序后规范化JSON计算；数据hash口径见`evaluation.provenance/digest`，不等于简单拼接或任务文件原始字节hash。

## 7. Token Usage 示例与 Baseline 状态

独立Mock取证命令（代码提交后运行，**1 passed in 0.39s**）：

```bash
.venv/bin/python scripts/run_offline.py pytest -q \
  tests/test_deepseek.py::test_mock_multistep_tool_ids_and_usage \
  --basetemp=.local/stage1/mock_evidence
```

本地原始证据：`.local/stage1/mock_evidence/test_mock_multistep_tool_ids_a0/c0c0c34621904982a9a72bcee10eb00f/`。公开摘要在`artifacts/stage_1_summary.json:mock_usage`。

| Mock响应 | prompt_tokens | completion_tokens | total_tokens | cache_hit / cache_miss |
|---|---:|---:|---:|---:|
| 资格提议 | 100 | 10 | 110 | 70 / 30 |
| 退款提议 | 100 | 10 | 110 | 70 / 30 |
| 最终回复 | 100 | 10 | 110 | 70 / 30 |
| 总计 | **300** | **30** | **330** | 不再次加入prompt |

这些数字是HTTP Mock响应中指定的协议夹具，来自真实SDK解析；**不是实际DeepSeek账单/效果数据**。归一化usage与原始token_usage同时保留。缺失任一调用的字段，则任务对应总量为null，另存known小计和unknown调用数；显式0才为0。参数化测试包含部分usage缺失，避免SDK自动补零误导。

Baseline当前状态：

- B0原始版本可由tag追溯；B0-compatible代码为本阶段代码SHA，仅追加适配和仪表。
- offline的7/7、Mock协议结果可复现；不能对外称DeepSeek任务成功率100%。
- 真实DeepSeek：`not_run`，N=0；真实成功率/SOP/Token/成本均未测；成本字段null。
- retries默认0；若用户以后调高，模型调用数仍为逻辑调用，不能当HTTP请求次数。所有失败调用缺失usage时保留unknown，不假设免费或零Token。
- Stage 5配对B1/M/MS实验尚未启动；没有优化收益结论。

## 8. Architecture & Directory Audit

当前Git交付结构（相对Stage 0；A新增，M修改；其余不变），最终44个跟踪文件：

```text
skillmem-agent/
├── .env.example [M]
├── .gitignore / .python-version / LICENSE / pyproject.toml / requirements.lock
├── .github/workflows/ci.yml [M]
├── README.md [M]
├── scripts/run_offline.py [M]
├── src/support_agent/
│   ├── models.py / evaluate.py / service.py [M]
│   ├── evaluation.py / tracing.py [A]
│   └── __init__.py / __main__.py / api.py / graph.py / tools.py /
│       policy.py / policies_kb.py / store.py / offline_model.py
├── tests/
│   ├── conftest.py / test_api.py / test_conversations.py / test_policy_and_tools.py
│   └── test_deepseek.py / test_evaluation.py [A]
├── data/
│   ├── store.json / policies.md / scenarios.json
│   └── eval_tasks.json [A]
├── docs/
│   ├── architecture.md / implementation_plan.md / upstream_README.md
│   ├── evaluation_protocol.md [A]
│   └── reports/phase_0.md / phase_1.md [A，仅phase_1]
├── artifacts/
│   ├── b0_manifest.json / stage_0_summary.json
│   └── stage_1_summary.json / stage_1_trace_sample.json [A]
└── examples/scenarios.txt
```

- 单一`support_agent`包，新增模块有实际调用与测试；没有框架层级、第二Agent或未来空模块。无删除/搬迁文件。
- State schema diff：无，仍MessagesState，不需checkpoint迁移。Graph nodes/edges diff：无。
- B0不变文件核对17/17：graph/tools/policy/store/offline、原4测试、原3数据、锁/pyproject/LICENSE、批准的两份规范均与Stage 0逐字节一致。原文架构目标不冒充当前实现。
- `service.py`仅8行观测注入，无审批分支/状态/异常处理修改。数据库关闭只由evaluation负责其拥有的临时连接，不改原Service生命周期。
- 与示意目录差异：独立新增`tracing.py`承载通用callbacks与计量，使Service不依赖评测器，避免evaluation超过约600行；这是用户明确允许的“必要Tracing适配”。新模块为276/369行，符合小规模职责边界。新增`test_deepseek.py`独立验证Provider，避免挤入业务测试。
- `data/eval_tasks.json`只含7个Stage 1开发夹具，明确不是提前完成Stage 5冻结Final集。
- `.gitignore`无需新增规则：Stage 0已覆盖`.env`、`.local`、sqlite/db/WAL、原始jsonl/log；6种探针均忽略，`.env.example`明确放行。
- 代码提交41个文件凭据模式扫描0命中；增加报告与2个公开摘要后，最终44个跟踪文件再次扫描0命中。无Key、原始日志、运行DB、research或venv进入Git。模式扫描不是通用PII保证，公开轨迹另做字段投影与人工复核。
- 原MIT License、作者和上游出处保留。研究快照未操作。

## 9. 风险、遗留项与 Stage 2 前置条件

1. **真实API未验证**：BaseChatOpenAI工具协议通过Mock，不保证网络、账户权限、模型可用性、实际用量字段或生成效果。真实smoke须有效环境凭据及明确预算；不需要再在聊天粘贴Key。
2. **B0已知可靠性问题保留**：内存业务Store、审批pending先删除后resume、同thread竞争、多interrupt、重复调用恢复、工具错误后规则模型重复提议。Stage 2处理，不掩盖为已修复。
3. **计量边界**：callbacks记录逻辑模型调用；SDK retries>0时无每次HTTPattempt统计；未获得usage的成本未知。工具重入不等于副作用；真实退款以Store判定。
4. **有限Oracle/SOP**：当前三家族、固定事实/短语匹配；未知语义误判仍可能存在。只有退款前资格查询软SOP，未声称全量安全/流程评测完成。
5. **日志隐私与可扩展性**：原始消息及Store快照留本地，Key脱敏不等于PII匿名化；Trace在内存保留事件，适合当前小规模脚本，超长评测需评估内存。单Recorder面向单任务；不声称Service多会话并发完整追踪能力。重复call_id的ToolMessage去重存在异常协议边界。
6. **依赖和平台**：锁定Python3.12.9/Linux；依赖升级需重新验证callbacks时序、工具schema、usage默认值和请求字段。未修改原wheel包外data问题。
7. **远程交付受阻**：GitHub写入认证未配置，已完成本地代码/报告提交；远程CI未运行。

进入Stage 2必须：用户审查并确认本报告和本地提交；维持原回归与本阶段G1测试；统一可靠性修复另立B1，不将修bug收益归因Skill/Memory。本轮到此停止。

## 10. G1 Gate 判定

| G1条目 | 判定 | 可核验证据 |
|---|---|---|
| 原24pytest、11场景继续通过 | PASS | 58项总回归含原24；原11 CLI；原测试/数据哈希不变 |
| DeepSeek Mock Tool Calling、ID、Usage | PASS | `tests/test_deepseek.py`15项；含字段/多轮/审批恢复/缺失usage/错误 |
| 三类业务Oracle正误结果 | PASS | `test_oracle_positive_and_corrupt_outcomes`及审批证据测试、7开发任务 |
| Runner结构化轨迹与统计报告 | PASS | 本地JSONL/CSV/summary；公开stage_1_summary与30步脱敏投影 |
| 每任务状态/模型/实际工具可追溯 | PASS | task.state、model.*、tool.*、interrupt/resume、SHA/哈希；独立Store/DB/thread |
| Key/敏感原始日志/数据库不进Git | PASS | 禁网入口、凭据脱敏测试、忽略探针、提交清单/模式扫描与公开投影复核 |
| 架构与目录组织 | PASS | 单package、无空模块；graph/state不变；Service仅8行观测注入 |
| 真实API状态诚实标记 | PASS（not_run） | 无批准预算，0真实调用；Mock与offline来源明确 |
| Git与阶段报告 | PASS（本地） | 代码SHA/最终stage-1标签；GitHub未推送原因明确 |

**Stage 1完成并停止，等待审查；不进入Stage 2。**
