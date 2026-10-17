# Phase 4 Report — Skill Routing 与 Progressive Disclosure

日期：2026-10-18（Asia/Shanghai）。**G4 PASS**：第二轮独立Review/Test通过，全量354项离线测试通过。付费请求0，MS真实效果not_run；按用户连续授权进入Stage5。

## 完成范围与版本

实现四Skill Registry、按需SOP/工具Schema、同一模型选择工具select_skill、执行层allowlist、审批冻结/恢复后切换、跨Skill记忆保持和路由成本分项。B0/B1/M独立保留；MS完整复用B1可靠执行和M上下文。没有新增Router LLM、Planning、LLM摘要或业务Policy。

- Repository：https://github.com/njuxushuo/skillmem-agent；Branch：main。
- 起点/Stage3标签：`175b4a4ca60255f44c3689cbaf7acf7c5cec0a42`。
- Stage4实现提交：`ed0755706edc024fb5f3b51a95e94072e2002716`。
- 报告/脱敏摘要单独提交；完整交付SHA以`git rev-parse stage-4`核验，保留既有标签不移动。
- 正常HTTPS写入仍缺认证，阶段提交/标签本地保留；对应新远端CI not_run。未force push、未绕过认证。

## 核心实现与调用链

| 文件/函数 | 实现 |
|---|---|
| skills.py:SkillSpec/load_skills | 四个固定ID、名称、简述、SOP路径/文本、tool_names；模型不能把输入变成文件路径 |
| skills.py:allowed_tool_names/render_skill_index/select_skill | 公共能力、未知状态fail-closed、简短索引、控制工具Schema |
| data/skills/*.md | order_shipping/refund/cancel/policy_support四份版本化SOP；工具与Policy仍是业务事实权威 |
| graph.py:build_graph/agent | 每轮根据active_skill重新bind_tools，Context计入索引、当前SOP及实际Schema |
| graph.py:guarded_tools | 先单调用/ID/参数/allowlist校验，再选择或业务执行；同一控制路径与预算 |
| service.py:SupportService | MS启M＋Skills，full_sop显式知识公平对照，不复制工具/审批实现 |
| tracing.py:statistics | 业务、控制、切换、路由模型及usage子集分别计量 |
| evaluation.py/evaluate.py | MS与full_sop入口、有效配置、SOP内容哈希；唯一原Oracle继续复用 |
| offline_model.py:_generate/_respond | 仅绑定select_skill时增加规则协议演示，依据同一Registry选择；B0/B1/M原行为保留 |

四Skill映射：

| Skill | 业务工具 |
|---|---|
| order_shipping | list_my_orders, get_order, track_shipment |
| refund | list_my_orders, get_order, check_refund_eligibility, request_refund |
| cancel | list_my_orders, get_order, cancel_order |
| policy_support | search_policies, escalate_to_human |

所有状态另有公共select_skill/escalate_to_human。初始无Skill仅暴露公共工具；选择成功返回同call_id的ToolMessage并写active_skill，下一次模型请求才加载目标SOP/Schema。选择不等于客户同意或员工批准；模糊需求可以直接澄清。跨Skill不清空TaskMemory，换订单仍执行Stage3焦点/资格规则。

ToolNode保留原8业务工具，但Guard独立检查允许列表，猜中工具名也不执行。多调用全部ID配对错误且零执行；未知Skill拒绝可在剩余预算内改正；相同Skill再次选择会消耗工作量但不算真实切换。控制选择与业务调用共享每轮8工具尝试，路由多出的模型调用也占8模型限制，没有给MS额外工作预算。

Service拒绝pending期间新chat；Guard再冻结选择。审批完成依据完整checkpoint中精确匹配的退款终态ToolMessage解锁，不依赖过期registry快照。旧B1/M待审checkpoint迁MS时，仅为匹配可信审批的消息ID/call ID/订单恢复refund范围，记录skill.restored；不是模型选择，不计模型路由。原GraphInterrupt保持。

## 计量及公平对照

full_sop=True只允许B1/M，使用与MS完全相同四份SOP全文，前缀明确全部工具已可用、忽略选择指令。MS拒绝full_sop；默认B1/M提示不变。后续主要B1/M/MS消融须同时给出B1＋全SOP对照，不能将新SOP知识归为路由优化收益。

| 指标 | 定义 |
|---|---|
| tool_proposed_count | 模型全部提议，含选择和非法JSON |
| business_tool_proposed_count | 提议中剔除select_skill |
| tool_executed_count | 原业务函数真实入口，含审批暂停/重入 |
| skill_routing_count / control_executed_count | Guard后实际进入选择逻辑，含未知Skill拒绝 |
| total_tool_executed_count | 业务入口＋控制入口 |
| skill_switch_count | 成功且active_skill确实改变；重复选择不算切换 |
| skill_model_calls | 响应中含有效或可识别非法select_skill的模型响应数，每响应只算1次 |
| skill_input/output/total_tokens | 上述响应的usage子集，已包含在总usage，不能重复相加 |
| skill_routing_latency_ms | 仅本地选择处理耗时；路由模型耗时在model.response和总任务latency内 |

Mock真实SDK例：7模型响应、700输入/70输出/770总Token；4个路由响应合440Token；4控制入口、1未知Skill拒绝、2次真实切换、1业务入口、总执行5。6个Tool Call ID全部闭合。此为协议夹具数字，**不是实际DeepSeek消融结果**。

非法JSON选择也计路由模型成本，但Guard拒绝导致控制执行0；混合同响应2非法＋1有效选择只计1次模型usage、3个错误ToolMessage；缺usage保留null与known小计，不补0。

## 测试与独立Gate过程

```bash
COVERAGE_FILE=.local/stage4/.coverage .venv/bin/python scripts/run_offline.py pytest -q \
  --basetemp=.local/stage4/tmp_final --cov=support_agent --cov-report=term-missing \
  --cov-report=json:.local/stage4/coverage_release.json
SKILLMEM_VARIANT=B0-compatible .venv/bin/python scripts/run_offline.py scenarios
SKILLMEM_VARIANT=B0-compatible .venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=B1 .venv/bin/python scripts/run_offline.py scenarios
SKILLMEM_VARIANT=B1 .venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=M .venv/bin/python scripts/run_offline.py scenarios
SKILLMEM_VARIANT=M .venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=MS .venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

| 检查 | 结果 |
|---|---|
| 最终全部pytest | **354 passed，39.51s**，0失败、0跳过 |
| 原Stage3 | 249项保留 |
| 新增105 | test_skills.py 71；test_stage4_acceptance.py 29；test_skill_measurement.py 5 |
| 独立Test最终 | 全354通过23.30s；独立29＋计量5通过2.48s；额外3SDK探针通过 |
| 独立Review最终 | 105专项通过5.55s；额外4计量探针通过 |
| B0/B1/M场景与任务 | 各11/11、7/7 |
| MS开发任务 | 7/7，规则模型 |
| MS原业务场景 | 11/11；比较原业务工具序列时剔除额外select_skill，选择仍单独计数，不声称原精确序列相同 |
| 语句覆盖 | 1850/1976 = 93.6235%，非分支覆盖 |
| Lint/format/依赖 | 通过，36 Python文件；依赖锁未改 |
| 新真实API | 0；模型质量、真实Token改善均未测 |

专项覆盖4映射/动态wire Schema、猜名越界零副作用、未知/路径式Skill ID、非法参数、多调用全配对、跨订单/跨Skill事实失效、模糊澄清、共享执行预算；批准/拒绝×恢复前后异常、4个额外查询后切换；B1/M待审批迁移MS12组合及独立4组合。

**Round1 Review FAIL / Test PASS**：初始351全回归通过，审查发现非法JSON select_skill位于invalid_tool_calls，路由分项遗漏，总usage仍正确。补3个SDK反例后首次文本替换未应用，测试真实2失败3通过/完整352通过2失败，保留日志；随后精确修补同时检查有效与无效调用，并按响应去重。

**Round2 Review/Test PASS**：354全绿，非法-only、混合、缺usage与非法业务工具不误算路由全部独立复核。没有修改业务Policy或放宽断言。

本地证据：`.local/stage4/{review.md,test_review.md,pytest_first.log,pytest_before_patch.log,pytest_release.log,coverage_release.json}`及场景/任务/SDK探针。公开摘要`artifacts/stage_4_summary.json`。原始对话和数据库不提交。

## Architecture & Directory Audit

新增应用仅skills.py（含固定元数据和控制Schema）；新增data/skills四SOP；新增三份测试。修改graph/service负责组装与配置，tracing/evaluation/evaluate负责共用计量与入口；offline_model小幅协议适配。无第二模型Agent、无新依赖、无服务层级或空模块。

应用18文件3328行→19文件3571行，净增243；测试12文件2854行→15文件3803行，净增949；scripts不变。graph321行、service502行，后者仍为既有registry/审批编排，新增配置接线未扩散业务责任。skills不反向依赖graph/service，不访问Store或退款；context引用固定SOP字符串，不修改数据库。工具/Policy和业务数据均未改。

API Key模式及本地Key精确匹配扫描72个候选跟踪文件无命中；.env/.local/DB/rawlogs继续忽略。MIT和上游来源保留。完整SOP在初始化时读取固定本地文件，**按需披露指模型输入只含目标正文**，不是声称文件系统从未读取其他SOP。

## G4 Gate与限制

| 要求 | 判定与证据 |
|---|---|
| 四Skill映射与动态工具Schema | PASS：Registry＋四组真实SDK/捕获模型请求断言 |
| 执行allowlist，越界无副作用 | PASS：全量ToolNode外Guard猜名退款拒绝、快照不变 |
| 完整Tool Call/ToolMessage协议 | PASS：选择、坏参数、多调用、审批恢复逐ID断言 |
| 跨Skill记忆与订单切换 | PASS：task保留、旧资格失效、新焦点不覆盖 |
| 审批冻结/恢复不重放 | PASS：批准/拒绝/前后故障、重复决定、旧checkpoint迁移 |
| B1/M独立与SOP公平对照 | PASS：原249＋11/7回归、显式full_sop元数据与同文本断言 |
| 路由全部开销计入 | PASS：Round2非法JSON修复、usage未知保留、总量/分项分离 |
| 独立审查/本地报告及版本 | PASS：第二轮两角色分别验收 |
| 远端推送/本次CI | BLOCKED / not_run：认证缺失 |

8工具小场景中路由可能增加成本；目前没有真实降本或成功率提升结论。离线模型固定规则不具备通用多轮语义能力。审批/SOP文本不改变硬Policy，复杂用户意图与约束仍可能被真实模型误解。Stage5需冻结独立任务/Oracle、相同模型预算和SOP知识对照，并保留全部失败；不新增Planning/摘要以混淆主要消融。
