# Phase 3 Report — TaskMemory 与 Context Engineering

日期：2026-10-18（Asia/Shanghai）。**G3 PASS**，第三轮独立代码审查和独立测试通过；按本轮用户授权继续Stage 4，不再等待阶段人工确认。新增真实API请求0；M在线效果为not_run，不用Mock效果代替。

## 目标与实际完成

新增M变体：结构化任务记忆、可信Observation、完整工具消息分组、预算内模型输入视图。B0-compatible/B1独立保留；B1的SQLite、幂等、审批、串行和执行预算共同复用。没有实现Skill、Planning、LLM摘要或长期用户画像。

三个策略：full_history保留全部历史；legacy_trim按最多40消息且完整工具组裁剪；structured_memory投影任务状态、压缩可安全投影的长工具结果，再删最旧非必留组。前两个是M中的策略对照，超估算窗口明确失败；B1自身仍走原策略，不被这些对照改写。

预算包含固定System、Skill/SOP预留接口、工具Schema、TaskMemory、历史、输出Token及余量。默认总估算窗口16384、输出按模型max_tokens（默认1024）、余量512；UTF-8字节加封装余量是保守估算，不是精确DeepSeek tokenizer或服务商硬Token保证。固定规则、最新请求、最新结果组放不下时明确context_budget_exceeded，模型请求数0；不偷偷丢弃关键内容。

## Git与源码

- Repository：https://github.com/njuxushuo/skillmem-agent；Branch：main。
- 起点HEAD/main/已fetch的origin/main：`57449bdcb2d33917aaecb6a064fe52d2bb6e67e9`。
- Stage 3实现提交：`9c4662687d866080a2742d64b8c15faa0d8e3093`。
- 本报告/脱敏摘要另行提交，完整交付通过`git rev-parse 'stage-3^{commit}'`核验，避免本文自引用自身SHA。
- 既有B0/B1标签不移动；原上游和MIT来源不变。
- 正常HTTPS推送预检失败：无法读取Username（terminal prompts disabled）。本地提交/标签保留；新提交远端CI为not_run，不以历史CI冒充。未强推或绕过认证。

## 核心实现与调用链

| 文件 | 核心接口与职责 |
|---|---|
| src/support_agent/state.py | SupportState、TaskMemory、empty_task：JSON-safe字段、安全默认、无授权身份字段 |
| src/support_agent/memory.py | user_memory_update、merge_user_constraints、invalidate_order_facts、observe_tool_results、render_task_context：纯数据更新与可信来源投影 |
| src/support_agent/context.py | ContextBudget、message_groups、build_model_context、_pending_approval_view：预算、完整工具组、当前审批与完整历史核对 |
| src/support_agent/graph.py | build_graph/agent/observe：用户状态→Context→模型→共同Guard→原ToolNode→观察→模型 |
| src/support_agent/service.py | SupportService：选择M与预算配置、读取可信审批快照；复用原恢复，不在中断时update_state |
| src/support_agent/tracing.py | context.built/context.measured与模型call_id关联；估算和actual_input_tokens/usage_source分列 |
| evaluation.py / evaluate.py | 开放M评测入口、保存有效context配置和共同ExecutionLimits |
| scripts/run_offline.py | 临时SUPPORT_DB，防API模块级演示app在选择性测试时打开本地运行数据库 |

工具事实必须由配对的AI Tool Call与ToolMessage产生。用户或模型声称退款不产生verified事实；工具拒绝/错误不当成成功。每组事实有source_tool、source_tool_call_id、提议消息作用域和观察位置。去重身份为AI消息ID+tool_call_id，既保证恢复不重复，又不丢掉后续合法复用供应商ID的结果；同一身份内容冲突使对应事实失效。

用户明确订单焦点优先；非当前订单结果只记录历史事实，不覆盖焦点、不激活旧约束、不回灌旧资格。资格标为历史快照，原Policy执行时仍重验。金额约束保留operator和完整原句；显式限制独立保留，普通引用只保留最近4条。这是有限中英文规则，不声称通用语义抽取；关键内容太大宁可明确失败。任务完成只由实际退款/取消结果推进，后续新业务请求重新进行。

审批源自registry/interrupt，恢复调用中固定快照与完整checkpoint的对应提议消息、call ID、订单及真实终态ToolMessage核对；不会因最近4条观察滚动而复活旧审批。旧B1待审批checkpoint可直接恢复到tools，observe先补未处理用户状态，再应用之后的工具结果，保证因果顺序。

完整checkpoint消息不删除、不替换为摘要。只返回TaskMemory更新和派生输入视图；原service按完整消息长度统计本轮行为继续有效。新增observe图节点后，有限recursion_limit按节点拓扑调整，主限制仍是B1的8模型/8工具尝试，不给M更多模型工作预算。

## 测试证据

环境：CPython3.12.9/Linux，requirements.lock未改，无新增依赖。以下入口清除API凭据并禁止socket连接。

```bash
COVERAGE_FILE=.local/stage3/.coverage .venv/bin/python scripts/run_offline.py pytest -q \
  --basetemp=.local/stage3/tmp_release --cov=support_agent --cov-report=term-missing \
  --cov-report=json:.local/stage3/coverage_release.json
SKILLMEM_VARIANT=B0-compatible .venv/bin/python scripts/run_offline.py scenarios
SKILLMEM_VARIANT=B0-compatible .venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=B1 .venv/bin/python scripts/run_offline.py scenarios
SKILLMEM_VARIANT=B1 .venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=M .venv/bin/python scripts/run_offline.py scenarios
SKILLMEM_VARIANT=M .venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

| 检查 | 真实结果 |
|---|---|
| 改动前原回归 | 153 passed，11.04s |
| 最终全部pytest | **249 passed，28.72s**；0失败、0跳过 |
| 原153保留 | 原测试断言不改；旧91仍明确B0配置 |
| 新增96 | test_memory_context.py 66；test_stage3_acceptance.py 29；test_context_measurement.py 1 |
| 独立Test Agent最终全回归 | 249 passed，17.46s；独立29项5.40s；8个额外复现探针通过 |
| 独立Review Agent专项 | 96 passed，7.21s，R1–R5原反例全部关闭 |
| B0/B1/M原场景与开发任务 | 各11/11、7/7（规则模型，不是实际LLM成功率） |
| 语句覆盖率 | 1756/1878 = 93.5037%；不宣称分支覆盖 |
| Lint / format /依赖 | 通过，32 Python文件，pip check无冲突 |
| 真实API / 费用 | not_run / 无新增付费请求 |

### 独立审查三轮与真实失败

1. **Round1 FAIL**：非当前单工具/旧checkpoint回放覆盖最新焦点；金额上限原话随最近引用丢失；退款后4次查询挤出recent outcomes使pending复活；同单新任务继承completed。独立迁移反例1失败22通过。修复为用户焦点保护、金额原句与operator、完整checkpoint审批核对、新业务进度重置。
2. **Round2 FAIL**：原四项关闭，全回归237通过，但审查发现B1待审迁M直接从tools恢复，先记completed再首次解析旧Human导致in_progress。独立真实子进程六组合3失败3通过（批准三组合失败、拒绝通过）。没有放宽断言或把已有成功回归当成通过Gate。
3. **Round3 PASS**：observe先合并未处理用户消息再应用后续可信观察；批准/拒绝×无故障/恢复前异常/恢复后异常，包括真实子进程迁移，最终状态与模型投影、重复决定无额外模型/退款均通过。代码审查与独立测试分别确认，无第四轮或跳关。

开发中还发现LangGraph按过窄MessagesState节点注解只传messages，丢弃新状态；去掉过窄节点注解并加图级事实保留断言。选择性测试曾因API模块级app打开现存B1数据库而与B0配置冲突，禁网入口改用临时数据库；不删除用户运行库。

本地原始证据：`.local/stage3/pytest_release.log`、`coverage_release.json`、`review.md`、`test_review.md`、各轮探针与失败日志。公开摘要：`artifacts/stage_3_summary.json`；没有提交原始对话、DB或Key。

## Architecture & Directory Audit

```text
src/support_agent/{state,memory,context}.py       新增
src/support_agent/{graph,service,tracing,evaluation,evaluate}.py  接线更新
 tests/{test_memory_context,test_stage3_acceptance,test_context_measurement}.py 新增
scripts/run_offline.py                          测试DB隔离
.env.example / .github/workflows/ci.yml / README.md  配置、回归与说明
docs/architecture.md / evaluation_protocol.md / reports/phase_3.md
artifacts/stage_3_summary.json
```

唯一support_agent包；memory/context不依赖graph/service、不调用模型、不写业务Store。数据/Tool/Policy语义未改，未创建skills/planning空模块。三个必要模块职责和批准目标一致；必要Service/Evaluation/Tracing接线不构成另一个Agent架构。架构文档追加已实现边界，保留原规范。

应用15文件2588行→18文件3328行，净增740物理行；测试9文件1756行→12文件2854行，净增1098行；scripts净增4行。不是把测试/文档计为应用代码。memory322行、context234行；service486行包含原registry和审批，接近规模提醒但职责相邻，未为行数拆空壳层级。

候选跟踪文件凭据模式及本地Key精确值扫描无命中；.env/.local/DB/raw JSONL均忽略。MIT及上游来源保留。源码和测试内容哈希由公开摘要保存，报告提交SHA通过stage-3标签核验。

## G3 Gate与剩余风险

| 门禁 | 判定/证据 |
|---|---|
| 超长轨迹真实触发裁剪 | PASS：88条checkpoint、42/45轮限制保留、巨型结果、dropped_message_groups断言 |
| 相同输入确定性Context | PASS：纯函数重复输出一致，原state/messages深拷贝不变 |
| 无孤立工具消息 | PASS：完整多调用组同进同退，4类破坏协议明确拒绝 |
| 旧checkpoint安全默认 | PASS：完成态迁移、真实子进程待审迁移及前后故障6组合 |
| 来源可信与订单切换 | PASS：用户伪造无verified事实、错工具/错订单不写入、错焦点不覆盖、去重冲突处理 |
| B1行为不受影响 | PASS：153原测试、B0/B1各11场景7任务，共同安全层 |
| 预算、安全规则、原始历史 | PASS：系统/Schema/输出均计入；超预算零模型请求；checkpoint完整 |
| 审批/幂等/原Usage不破坏 | PASS：原故障测试＋新增迁移、重复决定和Mock真实SDK usage关联 |
| 独立审查与本地交付 | PASS：第三轮Review/Test分别通过、版本与报告可追溯 |
| 远端推送/本次CI | BLOCKED / not_run：正常写入认证缺失 |

限制：有限规则无法解释任意自然语言；大量约束或无法安全压缩的关键输出可能超预算并明确失败；完整checkpoint/去重水位随会话增长，不是无限长会话方案；Token估算不能当实际降本证据。本阶段没有M真实效果；Stage5需固定模型/预算/数据再比较。B1原消息裁剪的已知策略保留作为对照。下一阶段必须保留B1/M独立运行并在执行层落地Skill allowlist。
