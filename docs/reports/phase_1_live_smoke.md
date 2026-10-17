# Stage 1 真实 DeepSeek 冒烟补充验收

日期：2026-10-17。范围：Stage 1 在线补充、必要评测修正与回归；未进入 Stage 2。

**真实接入最终判定：PASS。** 一次执行、8次真实HTTP请求/8次模型响应；物流查询、小额退款、大额退款及人工审批恢复均产生正确业务结果。原始Oracle因表达格式误判0/3；保留原始失败结果，修正通用等价识别后，对同一真实轨迹离线复判3/3，新增API请求0。没有用Mock数据替代真实结果，也没有把这3个任务当成通用成功率实验。

**远程交付：受认证阻碍。** 原Stage 1提交已在GitHub且CI成功；本次修正/报告仅本地提交，新提交远端CI为`not_run`。技术验收PASS不代表远程交付已完成。

## 1. 版本与执行配置

- 仓库：https://github.com/njuxushuo/skillmem-agent ，分支 `main`。
- B0上游：`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`。
- Stage 1交付：`0a8112fb157218f8e4905dfb8bff22be452e8658`，本地标签`stage-1`指向该提交。核查远端main为此SHA，但远端`stage-1`标签缺失。
- 真实执行源码：`a6efda9813e8453eb5483124ad3ce7a0ff2c3e78`，执行时工作树干净。
- 通用Oracle修正与离线审计源码：`dd049278b1ec5c637400a60a75fc459c8da47204`，审计时工作树干净。
- 报告/脱敏产物由后续独立文档提交交付；其SHA通过`git log -1 --format=%H -- docs/reports/phase_1_live_smoke.md`查询，避免在提交内容中自引用尚未生成的SHA。最终回复另提供该SHA。

| 配置 | 实际值 |
|---|---|
| Provider / API | DeepSeek / `https://api.deepseek.com/chat/completions` |
| 请求模型 / 响应model_name | `deepseek-flash` / `deepseek-flash` |
| thinking / stream / temperature | disabled / false / 0 |
| max_tokens / timeout | 1024 / 60秒 |
| SDK重试 / HTTP重试 / redirects | 0 / 0 / false |
| Agent变体 | B0-compatible，原8工具全绑定；原System/历史/审批控制流 |
| 业务数据 | 原`store.json`合成数据、固定业务日期2025-10-01；每Case独立Store/临时SQLite/thread |
| 凭据 | 只从本地`.env`读入`DEEPSEEK_API_KEY`，文件0600且Git忽略；不记录请求头 |
| 真实请求窗口 | 2026-10-17 UTC；逐请求时刻见公开summary中的budget.attempts |

官方页面当天描述`deepseek-flash`别名对应DeepSeek-V4.1-Flash-1017；实际响应只确认别名，不宣称拿到了不可变模型版本。SDK元数据`model_provider=openai`/`type=openai-chat`表示兼容客户端，并不意味着调用了OpenAI；实际endpoint由transport强制校验。

## 2. 可执行预算约束

实现：`scripts/run_live_smoke.py:BudgetLedger/BoundedTransport/main`。官方价格只读核对来源：[DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing)，核对日期2026-10-17。

采用峰时USD/百万Token：输入未缓存0.30、缓存命中0.006、输出1.20。文档1M上下文向上取1,048,576；输出硬上限1024。发送每次HTTP前必须持久化预留：

```text
每次最坏费用 = (1,048,576 × 0.30 + 1024 × 1.20) / 1,000,000
             = 0.3158016 USD
允许发送 iff 实际attempt数 < 15 且 已用或预留累计 + 本次预留 <= 2 USD
```

这不是依赖本地近似tokenizer的预算。实际请求限制端点、模型、非思考、非流式、n=1、输出上限与function工具；不能用切换模型绕过价格。flock/线程锁、原子写入、fsync保证先记账后发送；崩溃保留预留，不凭“没收到结果”认定免费。仅获得完整有效prompt/completion/total且符合上限时，才用全部输入按cache-miss峰价结算释放余额。Usage缺失不补0、不释放预留；连续6个未知用量请求后第7个被2美元约束拦截。HTTP/网络错误停止后续发送，不自动重试。

账本`.local/stage1_live/budget.json`持久化`claimed=true`，拒绝再次运行本次smoke；不得删除账本以重跑。日期变化要求重新核价。保证以服务商公开单价与上下文/输出合同为前提，不包含税费/汇率或未来价格变化。

**实际：8/15请求，全部HTTP200，账本stopped=null，费用保守上界0.0030549 USD，远低于2美元。** 脚本退出1是初版业务Oracle误判，不是API错误。离线审计退出0。

## 3. 三任务结果、工具协议与状态

原`data/eval_tasks.json`未改。取`shipping_multi_turn/refund_small/refund_approved`，物流只使用最后一个物流问句以控制请求量；Case名称保留但**本次不是跨订单多轮用户对话验证**。工具结果驱动的多轮模型请求与审批恢复都真实执行。

| 任务 | 模型请求/响应 | 工具提议/函数入口 | 输入/输出/总Token | 任务耗时ms | 原始Oracle | 同轨迹离线复判 |
|---|---:|---:|---:|---:|---|---|
| 物流1002 | 2/2 | 1/1 | 1882/77/1959 | 3146.171 | FAIL：missing_fact:eta | PASS |
| 小额退款1005 | 3/3 | 3/3 | 3311/218/3529 | 5045.049 | FAIL：missing_reply_fact | PASS |
| 大额退款1001+批准 | 3/3 | 2/3 | 3070/185/3255 | 4510.011 | FAIL：missing_reply_fact | PASS |
| 合计 | **8/8** | **6/7** | **8263/480/8743** | **12701.231** | **0/3** | **3/3** |

耗时来自runner墙钟，包含本地工具和自动审批脚本；不代表真人审核等待。逐模型及HTTP耗时见`artifacts/stage_1_live_summary.json`。大额退款函数入口3次为资格1次、退款暂停1次、退款恢复1次；退款实际只新增1笔，不把重入当重复退款。

业务Oracle核查：

- 物流：真实回复包含订单1002、TRK-88213、October 3, 2025；与夹具ETA2025-10-03一致；完整Store前后相同。
- 小额：订单1005的refunded从0到18.00，恰好新增18.00 EUR退款，approved_by=policy，无额外业务变化；最终回复€18.00。
- 大额：订单1001的refunded在interrupt前仍为0；审批脚本实际批准同订单/金额89.00，reviewer正确；resume后恰好新增1笔89.00退款，无pending及其他业务变更；最终回复€89.00。

`audit_smoke()`按原工具Pydantic schema校验提议参数，并验证每条model.request里的AI tool_calls与ToolMessage无孤立/未完成配对，提议ID与最终observation一一对应。三Case的`protocol_errors=[]`；审批resume保留原退款tool_call_id。工具函数异常/API错误均为0；两个退款任务资格查询先行，软SOP为2/2，物流不适用为null。未对SOP不适用项计成功。

## 4. 初版误判与小幅修正

真实执行没有退款/审批失败，但初版`judge()`使用字面匹配，不能识别`2025-10-03`与`October 3, 2025`、`EUR`与`€`等价，导致三个Case标记`oracle_failure`。这与API失败或模型决策失败不同。

本次修正：

| 文件/函数 | 改动及理由 |
|---|---|
| `models.py:get_model` | 可选注入http_client，使本次请求经过预算transport；不改变默认Provider业务行为 |
| `tracing.py:statistics/on_llm_error` | 明确响应数/错误数、HTTP状态及预算异常来源，用于分离调用成功与任务成功 |
| `evaluation.py:sop_checks` | 非JSON/非对象资格结果不抛异常；只有`eligible is True`作为资格证据，错误结果另计 |
| `evaluation.py:deepseek_api_status/failure_category` | 无调用not_run；尝试后无响应failed；部分失败partial_failure；全部响应才api_succeeded。区分api_error/budget_limit/model_decision_failure/oracle_failure/setup_error/measurement_error |
| `evaluation.py:reply_fact_present/judge` | 通用有效英文日期→ISO归一化、EUR/€识别；错误币种/错误日期/非法日期仍不能通过；不加入按Case特判 |
| `scripts/run_live_smoke.py` | 有界HTTP、持久化账本、独立3任务与离线审计；不是新Agent框架 |
| `tests/test_live_smoke.py/test_evaluation.py` | 预算、错误状态、SOP格式容错、Oracle等价/反例、离线审计不能伪装真实PASS |
| README、测量协议、本报告、2个公开JSON | 更新真实API状态、证据口径和可复现命令 |

原始`live_result.json`与3个JSONL不重写，SHA-256见公开summary。离线审计用更新后的通用Oracle与**原始回复/完整业务快照/审批记录**重新判断；不是再次向模型提问。`audit_result.json`记录原SHA、新审计SHA、源内容hash、dirty=false以及新增请求0。未改Agent提示词、业务Policy、数据集答案或工具逻辑以强迫模型通过。

## 5. 一条完整脱敏真实 Action–Observation 轨迹

来源：真实大额退款任务；完整**30步事件投影**在[`artifacts/stage_1_live_trace_sample.json`](../../artifacts/stage_1_live_trace_sample.json)。ID一致化为ORDER-A/CUSTOMER-A/REVIEWER-A/TOOL-1/TOOL-2；省略System全文与无关订单行，保留每个事件、模型消息、工具参数/结果、配对、真实usage和审批前后状态。它是脱敏摘要，不冒充完整原始日志。

| 原始步骤 | 真实Action–Observation |
|---|---|
| 1–2 | 独立任务开始；用户请求损坏商品退款；ORDER-A总额89.00 EUR、refunded=0、refunds=[] |
| 3–5 | 模型请求1，提议`check_refund_eligibility(order_id=ORDER-A)`，ID=TOOL-1；usage=888输入/50输出 |
| 6–10 | 参数校验后函数入口；eligible=true、max_amount=89.00、needs_human_approval=true；同TOOL-1返回ToolMessage |
| 11–13 | 模型请求2收到资格结果，提议`request_refund(order_id=ORDER-A, amount=89.0, reason="Damaged item")`，ID=TOOL-2；usage=1016/76 |
| 14–17 | 函数入口，GraphInterrupt，paused；没有正常退款结果，也没有写退款 |
| 18–20 | Service awaiting_approval；graph.interrupt；审批脚本approved=true，REVIEWER-A；graph.resume，原Store仍未退款 |
| 21–25 | 同TOOL-2、不同invocation重入函数，成功返回refunded/89.00/EUR/approved_by=REVIEWER-A；ToolMessage与TOOL-2配对 |
| 26–28 | 模型请求3收到真实退款结果；usage=1166/59；最终回复已退款€89.00、原付款渠道、5工作日及REFUND-A；Service answered |
| 29–30 | Store恰好新增1笔退款，refunded=89.00，无pending；termination=completed。原Oracle为missing_reply_fact；离线修正复判true另存 |

该轨迹原始模型输入/输出均未使用Mock。公开投影保留原始task.end失败，不把历史事件改成成功。

## 6. 真实Token与费用

数据来自8个真实HTTP响应的usage，SDK归一化后与`response_metadata.token_usage`一同记录。输入8263、输出480、总8743；输入缓存命中5760、miss2503，二者相加为8263，不能再加到总Token。未知usage调用0。

大额任务逐调用例：888+50=938，1016+76=1092，1166+59=1225，汇总3070+185=3255。相应cache hit为640/768/1024、miss为248/248/142。完整其他任务计量见公开summary中的budget.attempts.usage。

| 费用口径 | USD | 证据/限制 |
|---|---:|---|
| 保守上界 | **0.0030549** | 全部输入按峰时未缓存价，输出峰价；用于硬预算结算 |
| 缓存感知峰价计算 | 0.001361460 | `(5760×0.006 + 2503×0.30 + 480×1.20)/1e6` |
| 周六非峰价计算 | 0.000680730 | 当天文档非峰价为峰价50%；只按公开费率计算 |
| 实际账单扣款 | **null / 未获取** | 没有账单接口/账户账单证据，不把理论计价冒充实际扣款 |

Mock中的固定100/10等用量仍只存在独立自动化测试，不参与本表。3任务/1次采样不能证明长期成本、泛化成功率或后续优化收益。

## 7. 回归、复现与CI

环境沿用Stage 0锁：CPython3.12.9/Linux，无新依赖，requirements.lock/pyproject.toml未变。

实际命令：

```bash
COVERAGE_FILE=.local/stage1_live/.coverage .venv/bin/python scripts/run_offline.py pytest -q \
  --basetemp=.local/stage1_live/tmp_final --cov=support_agent --cov-report=term-missing \
  --cov-report=json:.local/stage1_live/coverage_final.json
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

| 项目 | 结果 |
|---|---|
| 全pytest（禁网） | **91 passed in 6.65s**，0失败，包含原24、Stage 1此前34及本次33项 |
| 原客服场景 | **11/11**；原测试中也包含场景汇总，不能重复计算为额外pytest |
| 新离线开发任务 | **7/7** |
| 应用语句覆盖率 | **924/995 = 92.8643%**；此口径不包含scripts预算脚本，脚本另有Mock单测 |
| Ruff / 格式 | 全通过，23 files already formatted |
| pip check | No broken requirements found |
| 真实调用前回归 | 83 passed in 6.45s；其后格式修正/离线审计增加8项，最终91项 |
| 离线审计 | PASS；禁用socket后执行，新增API调用0；原始文件不变 |
| 原Stage 1远端CI | **success**，SHA `0a8112fb157218f8e4905dfb8bff22be452e8658` |
| 本次新增提交CI | **not_run**，未推送；不能用原CI冒充本次CI |

关键测试覆盖次数15拦截、2美元预留拒绝、缺失usage不释放、重启保留账本、重复smoke拒绝、错误模型/端点/参数拒绝、401/超时/redirect后停止、异常usage、SDK异常链预算分类、SOP非JSON容错、失败API状态、日期/币种正反例及审计禁止模型调用。全部是离线Mock，不增加上述真实8次。

只复查已保存证据（不会发起模型请求）：

```bash
.venv/bin/python - <<'PY'
import runpy, sys
runpy.run_path('scripts/run_offline.py')['configure_offline']()
sys.argv = ['run_live_smoke', '--audit-only']
runpy.run_path('scripts/run_live_smoke.py', run_name='__main__')
PY
```

原付费执行使用`.venv/bin/python scripts/run_live_smoke.py --execute --env-file .env`，当前账本已锁定，不应重新执行或清空账本；新验证必须另行批准。原始证据在`.local/stage1_live/live_result.json`、`budget.json`、`audit_result.json`及`9e2a3b7242574a67845d00507b7a82c6/1-{0,1,2}.jsonl`；测试日志为同目录`pytest_final.log/scenarios_final.log/tasks_final.log`。原始目录不进Git，其他机器可复现离线测试，但若没有这些本地原始文件不能独立重跑历史审计。

远端CI：[Actions run 39028290086](https://github.com/njuxushuo/skillmem-agent/actions/runs/39028290086)，公开API确认completed/success。推送预检报`could not read Username for 'https://github.com': terminal prompts disabled`；当前没有可用GitHub写入凭据，未尝试绕过认证。正常推送与预检均使用禁用交互提示的HTTPS方式，认证受阻后保持本地提交；未尝试绕过认证。最终文档提交SHA与最终推送结果同时见交付回复。

## 8. Architecture & Directory Audit、安全与剩余风险

相对Stage 1只修改`models.py/evaluation.py/tracing.py`、评测测试/文档，新增`scripts/run_live_smoke.py`、`tests/test_live_smoke.py`和2个脱敏artifact。预算与复核逻辑放在scripts验证入口，Agent运行模块不依赖它；model注入http_client只用于受控执行。单包、单Agent，无未来空模块，无新框架或依赖。

`graph.py/service.py/tools.py/policy.py/store.py/offline_model.py`与stage-1逐字节一致；原4测试、原3业务数据、依赖锁、MIT和两份批准规范不变。State schema/节点/边/checkpoint格式均无变化，仍保留B0并行工具调用及原审批语义。没有提前实现Skill、Memory、Context、Planning、单工具限制、幂等或可靠性改造。

`.env`权限0600且Git忽略；本地原始日志/SQLite/账本都在忽略目录。提交前检查跟踪文件凭据模式和本地Key精确值均无命中；公开轨迹做字段投影及ID匿名化，不提交完整Store/原始JSONL。原先历史Stage 1报告保留当时not_run事实，由本补充报告更新当前状态。

剩余限制：

1. B0业务内存Store、同thread并发、审批领取后恢复失败、跨进程/重复副作用等问题仍存在，本次单进程成功不能证明这些问题已解决。
2. Oracle仅新增有效日期和EUR等价形式，仍不完整识别否定/矛盾/所有语言；本轮修正发生在看过结果之后，透明保留前后判定，未来冻结评测规则仍需独立样本。
3. `sop_checks`仅覆盖退款前资格查询；不是完整安全审计。请求预算只适用于专用smoke transport，不宣称所有通用evaluate调用有费用保证。
4. 模型别名和价格可能变化，费用上界依赖当天公开合同；账单不可得。此次3任务不是统计稳定的Baseline评测。
5. 本地修正已通过回归，但GitHub新提交CI尚缺认证后的推送验证，远端stage-1标签也待推送。

## 9. G1 在线补充 Gate

| 条目 | 判定 | 证据 |
|---|---|---|
| 真实DeepSeek非思考/标准Tool Calling | PASS | 8次HTTP200、真实usage与模型响应；绑定原8工具 |
| 15请求/2美元可执行上限 | PASS | transport发送前持久化预留；真实8次、上界0.0030549；预算Mock边界测试 |
| 三任务与大额审批恢复 | PASS（同轨迹复判） | 原始0/3明确保留；格式修正后完整业务Oracle3/3，追加API0 |
| Tool ID/参数/ToolMessage/多轮 | PASS | 三任务protocol_errors=[]；30步审批事件与相同ID恢复 |
| API与模型/业务判定失败区分 | PASS | status/failure_category单测；原始oracle_failure不当成api_error |
| 非JSON SOP与真实usage | PASS | 容错正反例；8/8完整usage、未知不补0 |
| pytest/原离线场景 | PASS | 91/91、11/11；7/7开发任务 |
| 架构/凭据隔离 | PASS | B0业务核心未改、无Stage 2模块、秘密及运行产物未跟踪 |
| 原Stage 1推送/CI核查 | PARTIAL | commit已远端且CI success；stage-1远端标签缺失 |
| 本次推送/远端CI | BLOCKED / not_run | 缺GitHub写入认证；本地提交保留，不绕过认证 |

**Stage 1真实接入技术验收PASS；远程交付受阻。到此停止，等待用户审查，不进入Stage 2。**
