# SkillMem Agent：源码讲解、设计取舍与面试准备

本文区分原作者实现、本项目工程改造和尚未验证的效果。Stage 3/4 的历史验收见对应阶段报告；**Stage 5 结果见第12节及阶段报告；自动Oracle存在已公开误拒**。真实模型的成功率、Token、费用、时延必须引用冻结实验，不能用下面的设计动机代替结果。

## 1. 可以怎样介绍这个项目

> 我基于一个已有 LangGraph 电商售后项目，保留单 Agent Loop、八个客服工具、业务政策和人工退款审批。先建立可持久化、可恢复和可计量的共同基线，再增加会话内结构化任务记忆、按预算构造的模型上下文，以及四个 Skill 的动态 SOP/工具 Schema。最后用相同模型和预算做对照，区分可靠性修复、记忆和路由各自的影响。项目不包含多 Agent、微调、强化学习或独立 Planner，业务数据也是合成订单。

这段描述强调可定位的工作，不宣称“从零实现 LangGraph”“发明工具调用”，也不预先宣称动态路由降本。若面试需要成果数字，补充 Stage 5 正式报告的实际结果、样本数、失败数和实验配置。

## 2. 上游已有与本项目新增

上游固定提交：`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`，来源为 `nitanandsinghupw/langgraph-support-agent`，保留原作者 MIT LICENSE。原始取证为16个 Python 文件、1303物理行，其中应用12文件/1026行；该数字是上游快照规模，不是当前项目规模。

| 范围 | 上游已有 | 本项目改造 |
|---|---|---|
| Agent Loop | `StateGraph`、`agent ↔ ToolNode`、Tool Calling | 保留主循环；增加共同执行 Guard、M/MS 的 observe 与派生上下文 |
| 工具与业务 | 8个业务工具、订单归属、退款/取消政策、人工升级 | 复用；增强持久化、幂等回执和异常恢复，不新增真实电商/支付接口 |
| 人工审批 | 大额退款 `interrupt` 与 `Command(resume=...)` | 审批日志、稳定 approval_id、决定不可覆盖、重复请求恢复 |
| 会话 | MessagesState、SQLite checkpoint、按消息数裁剪 | M/MS 的 JSON-safe TaskMemory；保留完整历史，只改变模型输入视图 |
| 模型 | offline/openai/azure | DeepSeek 非思考 Chat Completions、Mock协议测试、真实链路冒烟 |
| Skills | 无 | 四个固定 Skill、同一模型 select_skill、动态 Schema 与执行允许列表 |
| 评测 | 24个原测试、11个单轮场景 | 原业务回归保留；独立机制测试、任务runner、trace、Oracle、对照批次 |
| 规划与长期记忆 | 无 | 仍未实现独立 Planner、长期画像、RAG、LLM摘要或多 Agent |

原24个测试中包含一个汇总运行11场景的测试，不能说成35个互不重叠的单元测试。参考 OWWZO/ai-agent 的分层思想，没有完整移植其 Java 架构或源码。

## 3. 先看哪些代码

| 文件 | 关键符号 | 阅读问题 |
|---|---|---|
| `src/support_agent/service.py` | `SupportService.chat/_run/resolve_approval/_resolve_b1/retry_turn`、`ConversationRegistry` | 身份、会话、串行、审批与恢复如何衔接？ |
| `src/support_agent/graph.py` | `build_graph`、内部 `agent/observe`、`guarded_tools`、`b1_route` | 模型如何进入下一轮？哪些调用被拒绝？ |
| `src/support_agent/tools.py` | `make_tools` 中八个工具 | 业务事实从哪里来？副作用在哪发生？ |
| `src/support_agent/policy.py` | `refund_eligibility/needs_approval/can_cancel` | 哪些是代码硬约束？ |
| `src/support_agent/store.py` | `Store.transaction/operation/remember/persist/refresh` | 状态和幂等回执如何原子保存？ |
| `src/support_agent/execution.py` | `ExecutionLimits/ExecutionControl/database_lock` | 每轮预算、截止时间和锁的边界是什么？ |
| `src/support_agent/state.py` | `SupportState/TaskMemory/empty_task` | 哪些状态进checkpoint，哪些身份不能由模型修改？ |
| `src/support_agent/memory.py` | `user_memory_update/merge_user_constraints/observe_tool_results/render_task_context` | 用户陈述与可信工具事实如何区分？ |
| `src/support_agent/context.py` | `build_model_context/message_groups/_compact_result` | 怎样裁剪而不破坏工具消息协议？ |
| `src/support_agent/skills.py` | `SkillSpec/load_skills/allowed_tool_names/select_skill` | 路由如何影响模型可见性和执行权限？ |
| `src/support_agent/models.py` | `get_model` | 如何配置 DeepSeek、超时和禁用SDK重试？ |
| `src/support_agent/tracing.py` | `TraceRecorder/usage_of/statistics` | 提议、真实执行、暂停和费用如何分别计量？ |
| `src/support_agent/evaluation.py` | `run_cases/judge/sop_checks/unsafe_effects` | 成功由回复还是业务后置状态决定？ |
| `scripts/run_benchmark.py` | `freeze/verify_manifest/claim_phase`、批次执行 | 怎样保证配置冻结与共享批次预算？ |

`ConversationRegistry` 就在 `service.py`，并没有单独的 registry.py。当前仍采用少量 Python 模块，未为角色名称建立大量类或服务层。

## 4. 一次请求的真实调用链

```text
CLI / FastAPI
  → SupportService.chat(customer_id, message, thread_id)
  → 身份与会话归属检查、会话串行、建立本轮执行预算
  → graph.invoke(..., thread_id/customer_id/deadline)
  → agent
      B1：原系统提示＋最多40条消息＋8工具
      M ：用户状态更新＋TaskMemory投影＋预算Context＋8工具
      MS：再按active_skill选择SOP与可见Schema
  → 模型返回
      普通回答 → END
      工具调用 → guarded_tools
        协议/参数/预算/Skill允许列表校验
        select_skill → 匹配ToolMessage＋active_skill更新
        业务工具 → ToolNode → 原Policy与Store
  → M/MS observe：从真实配对结果更新任务事实
  → 下一轮agent
```

退款超过政策阈值时，业务工具在执行副作用前 `interrupt`，Service 返回 awaiting_approval。人工通过 `resolve_approval` 提交决定，恢复同一条工具调用；成功结果再经 observe 回到模型。等待不是另一名 Agent，也不是循环轮询一个 Planner。

M/MS 新增 observe 会增加图节点跳数，因此不能把同一个 `recursion_limit` 当成各变体公平工作量。公平限制由共同 `ExecutionControl` 的模型和工具尝试次数约束，图递归限制按拓扑调整。

## 5. 为什么先做 B1，再做 Memory/Skill

原 checkpoint 能恢复消息，不代表真实业务数据和审批流程就能恢复。原 Store 在内存，若订单退款状态丢失，模型可能基于旧消息继续操作；若审批提前移除，异常后可能找不到可重试决定。

B1 把业务快照、操作回执和审批决定持久化，并统一执行限制。M/MS 共享这层后，实验中的差异才主要来自记忆/上下文或路由，不能把修复重复退款带来的改善称为“Memory效果”。

业务事务与 LangGraph checkpoint 不是同一事务。代码用稳定操作回执处理“业务已提交、checkpoint未提交”的恢复重放，避免重复副作用。它是本机模拟业务的可恢复设计，不是对真实支付服务的分布式 Exactly Once 承诺。

证据可读 `tests/test_persistence.py` 的跨进程重启、提交后进程退出和 checkpoint 保存失败测试，以及 `tests/test_reliability.py` 的重复审批、决定冲突、预算和协议测试。

## 6. TaskMemory 不是把对话再摘要一遍

`TaskMemory` 保存 `active_order_id`、`constraints`、`verified_orders`、`eligibility`、近期工具结果、`pending_action`、`status` 等字段。它和完整消息历史同时存在：前者是便于模型使用的任务投影，后者是审计和恢复依据。

三个关键约束：

1. **来源可信**：用户或模型说“已经退款”只属于未验证内容；observe 处理真实配对的工具结果，并保留 source_tool、call ID 等来源。拒绝和错误不是成功事实。
2. **焦点与时效**：用户明确切换订单后，旧资格和动作失效；旧订单结果可以保留为历史，却不能覆盖新焦点。资格是历史快照，执行退款时仍由 Policy 重验。
3. **重放有因果关系**：使用提议消息身份与 tool_call_id 区分观察，避免恢复重复写入，也允许服务商在后续合法请求复用ID。旧 B1 待审批恢复可能直接进入 tools，observe 先补用户状态，再处理其后的结果。

`merge_user_constraints` 是有限规则：明确订单、金额、比较符和部分中英文限制；重要条件保留原话。它不是开放域语义提取器；`confirmed=False` 的用户约束不能变成退款授权。任务 status 依据真实退款/取消结果变化，不以模型声称“完成”作为证据。

## 7. Context Engineering 的机制和代价

`build_model_context` 预算中计入 System、工具Schema、Skill/SOP、TaskMemory、历史、输出预留和安全余量。估算是 UTF-8 字节加封装开销，名称中不能省略“估算”。真实计费由 provider usage 衡量。

`message_groups` 把一次 assistant tool_calls 与对应 ToolMessage 作为完整组；不允许裁出孤立工具结果。`_compact_result` 对长JSON保留允许的业务字段，原始结果仍在checkpoint/trace。先确定性压缩，再删除最旧非必留组；系统规则、最新用户请求和关键最新组放不下时明确停止。

M 可使用三种策略做对照：`structured_memory`、`full_history`、`legacy_trim`。后两种超估算预算会失败，不额外偷偷裁剪。B1 仍保留原来的40消息策略，这里的40不是Token数。

没有调用额外 LLM 做摘要。原因是当前小任务可先用确定性投影，减少费用、事实漂移和效果归因混杂。保留字段 `context_summary` 不等于已有摘要功能。完整历史和去重记录继续增长，因此仍需未来设计归档和生命周期。

## 8. 动态 Skill 的实际含义

| Skill | 对应业务工具 |
|---|---|
| order_shipping | list_my_orders、get_order、track_shipment |
| refund | list_my_orders、get_order、check_refund_eligibility、request_refund |
| cancel | list_my_orders、get_order、cancel_order |
| policy_support | search_policies、escalate_to_human |

`select_skill` 和人工升级是公共能力。无活跃Skill时只暴露公共能力；选择成功后，下一轮才加载目标SOP并绑定目标Schema。所有SOP是固定本地文件，初始化已读取全文；“按需”指模型上下文披露，不是文件读取层面的懒加载。

只隐藏Schema不够，因为模型可以猜工具名。`guarded_tools` 在执行前再次检查允许列表；ToolNode即使持有全部业务工具，越界调用也不会执行。选择Skill没有改变订单归属、退款规则或人工审批权限。

同一模型负责路由，没有单独Router LLM。路由仍增加模型调用、Token与延迟，并消耗共同工具预算。对八个工具的小项目，收益必须测，可能出现负结果。`B1 + full_sop` 使用同样四份SOP全文作为知识对照，防止把更好的业务说明错误归因于Schema路由。

## 9. DeepSeek 接入要讲清的内容

`models.py:get_model` 使用 OpenAI兼容 Chat Completions 接口，通过 `BaseChatOpenAI.bind_tools` 传Schema（DeepSeek分支用该父类保留标准max_tokens字段）。当前配置使用非思考模式，模型名、base URL、输出上限、超时等可配置；SDK重试为0。没有把OpenAI兼容等同于所有模型模式都兼容。

需要保持 assistant tool_calls 与 ToolMessage.tool_call_id 成对，并保证审批恢复时仍是原调用身份。Mock与真实服务端冒烟覆盖有限链路；Thinking模式的 reasoning_content 回传不属于本版已验证范围。

真实 `usage_metadata`/response token_usage 与估算分开；缺usage为null。路由Token是总Token的一部分，不能把分项再次加到总数。API Key只从环境或受控本地入口读取，不写公开artifact；程序不会自动加载.env。

## 10. 如何解释评测设计

正式对照包含 B1、M、MS 和 B1＋全部SOP；使用相同模型配置、每轮预算、数据与审批脚本。原7任务属于开发回归；Stage 5 另有6个Dev和30个独立Final任务。真实运行计划及实际完成数必须以冻结manifest和阶段报告为准，不把计划值写成完成值。

`run_cases` 对每个任务创建独立Store、临时数据库和会话，保存before/after、逐轮结果、原始trace和元信息。长历史通过真正的graph reducer/checkpoint写入，只能包含完整user/assistant对，不能伪造tool结果或直接填写TaskMemory。

核心判定与计量：

- **任务成功**：查询同时要求已执行工具的事实与最终回复；退款/取消核查真实状态、金额、审批和无额外变更；政策、拒绝、澄清等使用显式规则。
- **SOP**：当前仅测“退款提议前成功资格查询”，不是四Skill所有SOP的完整评分。
- **不安全副作用**：单独检查越权或非预期业务变更；未完成退款是任务失败，不自动是危险操作。
- **Memory**：看最终回复和业务/工具证据是否保留预先指定事实，不要求基线也有TaskMemory字段。
- **Resume**：适用任务有计划分母，不能因模型未触发审批就排除失败任务；Memory任务初始化失败也不被悄悄剔除。
- **工具开销**：模型提议、业务函数真正进入、暂停/重入、Skill选择分别计量；真正进入次数可能多于唯一业务操作次数。
- **Token与费用**：provider usage为实测；mock/offline未知值保留null。共享批次账本另外控制预算，普通CLI并不自动具备同样费用上限。

Oracle允许部分数字/日期/货币格式变化及附近否定，但不能理解任意自然语言改写。Policy和拒绝匹配可能误拒正确表达；未做LLM-as-judge。Frozen Final失败后不调整提示、Agent或Oracle，否则破坏留出集意义。

只读工具故障是一次TimeoutError，审批故障是一次恢复前/后异常；Stage5重启夹具是同进程Service重建，不是进程崩溃。原有独立子进程测试可以作为另一类工程证据，但不能混成同一个实验结论。

## 11. 常见追问与准确回答

**为什么不直接用LangGraph checkpoint当Memory？** checkpoint保证状态保存，但长消息全部发给模型成本高，裁掉历史又容易失去关键约束。TaskMemory将来源可验证的关键状态明确投影；checkpoint仍是完整记录，两者用途不同。

**为什么还要在工具里重验Policy？** 记忆中的资格可能过期，模型可能选错参数；提示和Skill都不是业务权限边界。真正副作用仍以当前订单、政策和可信审批为准。

**为什么没有规划器？** 当前任务可由单Agent工具循环、SOP和进度状态处理。额外Planner增加调用和状态同步复杂度，也混淆主要对照。若数据证明复杂分支失败需要明确步骤，再单独作为消融实现。

**为什么不使用向量数据库？** 当前记忆是一个会话中的少量订单事实和约束，按ID结构化访问更直接。没有跨文档语义检索需求，不为技术清单增加依赖。

**是否做到生产级安全？** 没有。演示身份、合成业务、本机SQLite、协作式超时和规则Oracle都有明确边界；未接入真实支付、生产鉴权、灾备和容量方案。

**如果MS更贵怎么办？** 如实报告。路由额外轮次可能超过省下的Schema Token；可保留更简单的B1或M，不为了正结果调整Final集。优化是否值得由同条件任务成功、失败代价和总体开销共同决定。

**测试通过能证明真实模型正确吗？** 不能。离线测试证明协议、状态、恢复及判定器按设计运行；真实LLM效果必须由冻结在线实验评估，且结果仅适用于其任务、模型、预算和时间范围。

## 12. 真实实验结果与正确讲法

冻结代码`c63ec4d189830be94e30c9fc6dd3416ee6e9b97c`，30Final任务×4变体×3次=360runs，另12Pilot。1327次真实HTTP全部200，输入2,350,086/输出78,941/总2,429,027Token。峰价全miss上界$0.7997550、考虑缓存的峰价估计$0.461949294；实际扣款未知。

| 变体 | 冻结自动Oracle通过 | Final总Token | Memory规则通过 | 业务/Skill执行 |
|---|---:|---:|---:|---:|
| B1 |72/90（80.0%）|403,311|25/36|156/0|
| M |85/90（94.4%）|625,354|35/36|171/0|
| MS |78/90（86.7%）|711,512|33/36|153/100|
| B1_SOP |75/90（83.3%）|616,871|27/36|157/0|

这证明本批任务的权衡：M比B1多13次通过，但总Token增加55.1%；MS比M少7次通过，总Token增加13.8%，100次Skill选择也是实际成本。全SOP仅改善B1部分结果，不能证明路由本身带来提升。四组unsafe均0、适用审批恢复各6/6；这些是模拟业务和受控故障范围的证据。

自动评分有13条确定误拒（中文订单号12、拒绝同义词1），另6条过严订单编号要求和2条拒绝理由争议；因此不能在简历写“真实准确率94.4%”而隐去口径。应写“在冻结30任务×3重复的规则Oracle中，M85/90通过；公开全部失败与误判审计”。没有修改Final评分抬高数字。

## 13. 三个最重要的设计取舍

1. **先可靠性，后优化归因**：B1/M/MS共用幂等、审批与预算，避免把修bug的收益算成Memory/Skill。代价是SQLite全量快照与服务串行限制吞吐，适合小研究项目。
2. **确定性记忆投影，完整历史保留**：来源与订单边界可测试；不引入LLM摘要降低事实漂移。但规则提取范围有限、估算偏保守、历史仍增长，且本实验M净Token增加。
3. **动态Schema再加执行Guard**：减少当前工具暴露且防猜名越界；保留一个Agent。代价是额外选择轮次，在8工具任务上MS净收益为负，默认演示宜优先M，MS用于展示机制与反例。

## 14. 三个真实开发失败与修复

- Stage3：旧订单工具结果覆盖用户新焦点/旧资格污染。独立测试发现后，用户明确焦点优先、工具事实按来源订单关联，旧资格失效；相应回归在test_stage3_acceptance.py与test_memory_context.py。
- Stage3：旧B1待审批迁M直接进入tools，observe先写完成再被旧Human解析重置为in_progress。修复为因果顺序初始化用户状态，再消费工具结果；G3第三轮独立审查通过。
- Stage4：非法JSON的select_skill未计入路由模型Token子项（总量正确）。合并合法与invalid_tool_calls的识别，补SDK级计量测试；G4第二轮通过。

线上失败另行保留：长历史B1忘记早期金额/订单；M/MS有时已获退款指令仍再次追问确认；中文已正确回答却被规则Oracle误拒。前两者没有在查看Final后定向修复，第三类单独审计并保留冻结分数。不要把“识别原因”描述成“全部解决”。

## 15. 简历可准确描述的新增工作

可写：在保留上游LangGraph与8工具业务的基础上，完成SQLite幂等/审批恢复共同基线、来源可追踪的会话任务记忆、完整工具消息组的预算上下文、四Skill动态Schema与执行允许列表；建立Mock/离线/真实provider分离的Trace/Oracle与持久费用预留评测，564项自动化测试通过，公开冻结消融与失败审计。

这些是本项目相对上游的新增，不应声称原有Agent框架和工具均从零开发。开发中使用了Codex协作，个人熟悉和负责的范围应按实际掌握情况表达。未实现Planner、LLM摘要、跨会话长期记忆、微调、RL、多Agent或生产支付，不作为项目成果。
