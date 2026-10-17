# Stage 5：冻结消融实验与交付报告

日期：2026-10-18（Asia/Shanghai；真实调用与核价发生于2026-10-17 UTC）。

**G5独立技术Gate PASS。** 此结论覆盖离线回归、冻结协议、真实计量、业务安全和可复算实验；**不代表自然语言Oracle无误差**。13个确定误拒及8个输出要求/业务理由争议已单独公开。所有主分保持冻结时判定，不重标、不重跑付费任务。GitHub写入认证仍缺失，远端交付/新CI未完成；用户明确允许保留本地提交。

## 1. 实现与范围

完成新增6个Dev与独立30个Final任务，扩展多轮、合成历史、只读长结果、一次工具故障、审批恢复前后故障与同进程Service重启夹具；扩展Oracle、记忆/恢复适用分母和证据链。完成B1/M/MS/B1_SOP四组、三次重复的真实配对实验。规划器和LLM摘要未实现，当前任务无必要增加这些混杂因素。没有修改Stage4的Agent、Memory、Context、Skill或业务Policy。

与原建议目录的差异：原`data/eval_tasks.json`7任务保留，新增`benchmark_dev.json`/`benchmark_final.json`和manifest，避免覆盖历史。批次控制在独立scripts/run_benchmark.py，复用Stage1费用账本；没有第二套业务Oracle。SOP只测退款前资格检查，未声称全四Skill的自然语言SOP全部可自动判定。

## 2. Git与冻结

- Repository：<https://github.com/njuxushuo/skillmem-agent>；Branch：`main`。
- 固定上游：`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`，B0兼容：`0021b1d96a942bbd4259317bcfd5f494c33d7617`。
- B1交付：`57449bdcb2d33917aaecb6a064fe52d2bb6e67e9`；Stage4：`dee24d95b1b4071428951d3e7dfc05bfc506585a`。
- **Stage5真实执行代码SHA：`c63ec4d189830be94e30c9fc6dd3416ee6e9b97c`**。所有372条运行记录均为该SHA、clean工作树。
- 冻结protocol SHA256：`6798cdd3316d9bf34fc55739b552a23e0105d4fcd2d89bd35833dd4f2f58ee88`。
- Dev raw SHA256：`ae0225384938f393859965f73adb20675fab2f6ab76d1dc7d44bdf304093c5fc`。
- Final raw SHA256：`e0c8d22df58eb2eb95763c153a49e1e3faaa9dbf926d50277af6c3bb239c5c37`。
- manifest冻结54个源码/脚本/测试/数据/依赖文件；实际逐任务输入另存dataset_hash。所有代码与任务均在付费前提交。
- 本报告与公开证据是后续纯交付提交，标签`stage-5`指向该交付；可用`git rev-parse stage-5`查准确SHA，最终审查报告记录该SHA，避免文档自引用提交哈希。
- 正常push失败：`could not read Username for 'https://github.com': terminal prompts disabled`。没有强推、覆盖历史或绕过认证。新远端CI：`not_run`。

## 3. 核心实现与职责

| 文件/函数 | 本阶段内容 |
|---|---|
| evaluation.py：EvalCase/Expected/Approval | schema严格拒绝额外字段；受控订单覆盖、成对history/history_before、只读ToolFixture、恢复脚本 |
| evaluation.py：seed_history/fixture_tools | 通过真实checkpoint reducer注入合成历史；不预置TaskMemory/伪造工具结果。RLock包围全部评测Service构造，执行闭包每case隔离 |
| evaluation.py：judge/memory_checks/unsafe_effects | 查询需实际工具证据；退款/取消核查最终业务状态，政策/澄清/拒绝/安全失败显式规则；失败仍保留状态 |
| evaluation.py：run_cases | 每case独立Store/DB/thread，适用记忆/恢复任务初始化失败分母，不因未触发审批而排除；例外继续后续case |
| scripts/run_live_smoke.py：BudgetLedger/BoundedTransport | 原15次/$2默认保持；新增实例级受验证limits，Stage5共享$3/2000上限 |
| scripts/run_benchmark.py | freeze/verify_manifest、单次claim_phase、DatedTransport、轮换调度、Pilot Gate、结构化统计与白名单导出 |
| tests/test_benchmark_* | 53评测逻辑、125数据/Oracle、17预算、15独立预算审查，共210新增项 |

运行链：受限HTTP Client → get_model → run_cases → SupportService → 原B1/M/MS Graph/ToolNode → Trace/Store → judge → 聚合。评测4线程仅调度互相隔离的业务任务，不构成多Agent产品架构。

## 4. 执行配置、费用保护与实际用量

模型deepseek-flash，Chat Completions，thinking disabled，temperature0，max_tokens1024，timeout60s，SDK retries0。所有变体每用户轮8模型/8工具尝试、120s执行窗口、5s工具协作截止。M/MS显式structured_memory，估算总窗口16384、输出预留1024、安全余量512。B1_SOP是B1加与MS完全相同四份SOP全文。

每次HTTP发送前，将全1,048,576输入+1024输出峰价$0.3158016预留写入同一账本，持锁+fsync+原子替换。有效usage才把预留降低为实际Token的全miss峰价上界；未知usage/异常保留预留。API错误全局停止，phase只可领取一次，UTC核价日变化拒绝发送。价格来源与快照哈希已冻结于manifest。

| 范围 | 任务运行 | 真实HTTP | 实际Input | 实际Output | 实际Total |
|---|---:|---:|---:|---:|---:|
| Pilot |12/12|45|由逐行CSV复算|由逐行CSV复算|71,979|
| Final |360/360|1,282|2,280,807|76,241|2,357,048|
| 本轮合计 |372/372|**1,327**|**2,350,086**|**78,941**|**2,429,027**|

1327次全部HTTP200，真实usage完整，无API错误/未知usage/超预算/未运行项。缓存hit1,148,999、miss1,201,087，两者和恰等于input，不重复加总。

- 全输入miss峰价费用上界：**$0.7997550**，低于授权$3。
- 考虑真实缓存分区的峰价估计：**$0.461949294**。
- 实际账户扣款：**null，无法可靠取得账单**。上述均不是实付金额。
- Stage1历史8次调用不属于本轮授权账本；本轮无其他真实API调用。

账本原始数据只在`.local/stage5/budget.json`；公开核算为`artifacts/stage_5_budget_audit.json`。不会删除账本重跑本批次。

## 5. 冻结实验结果

30个Final任务按基础5/多轮7/长上下文6/Skill切换或澄清5/安全恢复7分层，每组3重复，所有计划任务计入分母。长历史是seeded synthetic context，不是模型真实生成的漫长对话。恢复是同进程Service重建；Stage2另有跨进程崩溃测试，不能混为本实验结果。

**下表是冻结自动Oracle通过率，不是已证明无误差的语义任务成功率。** 原始失败不删，13个确定误拒及其他争议见下一节。

| 变体 | 通过/总数 | 自动通过率 | Input/Output/Total Tokens | 模型调用 | 业务/Skill执行 |
|---|---:|---:|---:|---:|---:|
| B1 |72/90|80.0%|386,046 / 17,265 / 403,311|290|156 / 0|
| M |85/90|94.4%|605,237 / 20,117 / 625,354|324|171 / 0|
| MS |78/90|86.7%|690,550 / 20,962 / 711,512|374|153 / 100|
| B1_SOP |75/90|83.3%|598,974 / 17,897 / 616,871|294|157 / 0|

| 变体 | 资格SOP通过/适用 | Memory规则通过/适用 | 审批恢复 | Unsafe | 每任务平均/成功任务平均Total Tokens | 延迟均值/中位/P95秒 |
|---|---:|---:|---:|---:|---:|---:|
| B1 |21/21|25/36|6/6|0|4481.2 / 4423.9|5.611 / 5.544 / 9.957|
| M |26/26|35/36|6/6|0|6948.4 / 7004.2|6.980 / 6.168 / 12.366|
| MS |23/23|33/36|6/6|0|7905.7 / 8097.4|7.959 / 8.170 / 14.166|
| B1_SOP |21/21|27/36|6/6|0|6854.1 / 7016.0|7.568 / 7.665 / 12.820|

资格SOP只指成功资格检查先于退款提议；分母因模型是否真正提出退款而不同。未提出退款并不算SOP成功，任务失败照常保留；不能将100%解释为四份完整SOP全部遵循。Memory也受最终文字引用条件限制，不能当通用记忆能力分数。所有模型和Skill成本计入总量；MS路由响应100次、140,880Token是总量子集，不能再相加。

| 分类 | B1 | M | MS | B1_SOP |
|---|---:|---:|---:|---:|
| basic |15/15|15/15|14/15|15/15|
| multi_turn |19/21|20/21|18/21|21/21|
| long_context |3/18|14/18|12/18|3/18|
| skill_switch |15/15|15/15|15/15|15/15|
| safety_recovery |20/21|21/21|19/21|21/21|

配对按case_id+repeat：M相对B1有14次转为通过、1次转为未通过，净+13；总Token **+55.1%**。MS相对M无新增通过、7次转未通过，总Token **+13.8%**。MS相对B1_SOP新增9次/失去6次通过，Token+15.3%。因此本任务集优先采用M展示可靠任务执行，MS保留作动态路由机制与成本反例，不能宣称路由优于M或降本。

summary.json还保留Wilson区间与全部失败列表。重复case相关、小样本、规则误判和同一供应商时间窗口均限制统计解释；没有因果总体保证。这里没有对最终结果反复定向调参。

## 6. 独立判分有效性审计与未解决问题

两个独立审查角色逐条核对全部50个未通过任务，分类一致：

| 类别 | 条数 | 处理 |
|---|---:|---|
| 中文订单号词边界误拒 |12|`1002号订单`含正确编号，但Unicode `\b`不满足；确认是Oracle缺陷 |
| 安全拒绝同义词漏配 |1|`not able to share`未命中词表；实际订单访问已正确拒绝 |
| 正确使用订单但最终省略编号 |6|工具证据/物流正确，冻结输出要求未满足；不能仅据此称记忆丢失 |
| 拒绝恶意批准但缺具体业务理由 |2|安全目标满足，但冻结业务拒绝理由证据不足；不直接改为全任务成功 |
| 未恢复早期上下文 |18|追问已有订单/金额或遗漏早期要求，工作未完成 |
| 未执行已经要求的退款 |11|查询合格后再次确认，最终退款未发生 |

公开`artifacts/oracle_audit.json`逐条保留case/variant/repeat、原分、原因、审计分类与原trace哈希；去掉全部原始回复和个人字段。仅失败侧后验审计，未穷尽310条自动通过的语义假阳性，所以不得把主分简单称为“真实成功率的严格下界”。

没有修改冻结Oracle、提示、Agent或任务，也未追加真实模型重跑。未来应在独立新开发样例中修复中文数值边界/语义同义覆盖，再冻结新留出集；不能用已见Final集继续追求分数。本次交付的是可解释的有限规则研究结果，不是全面语义能力认证。

## 7. 轨迹交付

`artifacts/stage_5_traces.json`包含5条人工挑选、合成身份脱敏的真实Action–Observation投影，保留每次模型响应、工具提议/执行/观察、审批与业务变化及真实usage。原模型输入全文/完整快照不公开，只给计数与原trace哈希；不是伪装成原始完整日志。

1. Pilot d_pilot_02 / MS：选择退款Skill→资格→6 EUR退款→答复。
2. Final f_safe_03 / MS repeat1：大额退款interrupt→Service重启→注入恢复失败→同决定恢复→单笔业务退款。
3. Final f_long_01 / B1 repeat1：长历史后未恢复早期退款信息，任务未完成。
4. 同任务 M repeat1：TaskMemory保留早期要求，完成退款，展示与B1的配对差异。
5. Final f_safe_06 / MS repeat1：安全拒绝伪造审批，但未满足冻结业务理由rubric，原分仍失败。

这些是模拟订单与真实模型交互，不接入真实交易系统。

## 8. 测试与独立审查

```bash
COVERAGE_FILE=.local/stage5/.coverage .venv/bin/python scripts/run_offline.py pytest -q \
  --basetemp=.local/stage5/tmp_release_final --cov=support_agent \
  --cov-report=term-missing --cov-report=json:.local/stage5/coverage.json
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=M .venv/bin/python scripts/run_offline.py tasks
SKILLMEM_VARIANT=MS .venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
```

- 主流程最终：**564 passed，47.82s**；覆盖2144/2286语句，93.7883%。包括原24项和所有Stage1–4测试；Stage4为354，因此本阶段新增210。
- 独立Test：**564 passed，26.76s**；独立数据测试125；独立Review专项210；预算发送前47项（与全量重叠，不相加）。
- B0-compatible/B1/M各11原始场景、7开发任务通过；MS7任务通过，11原场景按业务序列验证通过（剔除select仅用于比较，另计路由，不声称原序列完全相同）。
- lint、format、pip check通过；所有离线测试禁网、清Key环境，真实HTTP只来自受限显式runner。
- 独立逐条重算372行Oracle/SOP/unsafe/调用/Token/trace hash，0差异；1327 HTTP账本与Trace一致。

开发中保留的失败与修复：全局fixture patch在并发构造时串用→RLock隔离；外部context策略可改变配置→显式固定；Pilot未实际resume也可能通过→12任务全通过及实际restart/resume证据；None计数导致预检TypeError→缺失计数按0拒绝。一次全回归记录562 passed/2 failed，修复后完整564通过。早期live_smoke复判因新Oracle强制事件出现1失败，保留无events的旧接口兼容后15项smoke全过。

以上在冻结前完成。G5独立Review第1轮与独立Test完成；没有超过三轮限制。真实结果的Oracle缺陷以审计披露，不用改Final抹去失败。

## 9. 架构、规模与安全审计

应用仍19个Python文件/4056物理行（含注释空行），相对Stage4 3571行净+485，全部增量在evaluation.py。Stage5脚本run_benchmark.py514行，预算复用文件净+28；新增测试4文件1017行，测试总19文件4820行。冻结实现提交11文件+4248/-48行，包含2036行新增JSON任务与测试/文档，不应称为核心应用代码量。

state/memory/context/skills不反向依赖service/graph，Graph组装，Service编排，Oracle在evaluation.py。没有空模块、第二应用包、额外运行Agent、数据库或新依赖。evaluation.py已超过原轻量目标，是明确维护风险；后续可在独立需求中拆分schema/Oracle，但不为当前验收大改结构。

Git忽略.env、数据库、完整日志、.local及研究快照；公开CSV采用字段白名单，5轨迹遮罩合成客户名/email，判分审计不公开原回复。凭据模式扫描未命中候选跟踪文件；MIT与上游历史保留。Key过滤不是通用PII匿名化，公开真实用户数据仍需单独审查。

## 10. G5证据表与后续条件

| 要求 | 结果 | 证据/边界 |
|---|---|---|
| 全部回归/关键业务安全 |PASS|564测试，原11/7，各组unsafe0 |
| 冻结独立Final与公平变体 |PASS|54文件manifest，独立30任务，B1/M/MS/B1_SOP共同限制 |
| 真实任务可执行Oracle含失败 |PASS（有限规则）|372行全部得分；13确定误拒公开，不作无条件语义正确认证 |
| 真实usage与调用/预算一致 |PASS|1327次全部200，provider真实2,429,027Token，独立全量复算 |
| 不伪造Token/成功率 |PASS|主分原样、失败审计、真实/Mock分离、实际账单null |
| 结果可追溯/复算 |PASS|c63ec4d、protocol/data/trace哈希，公开CSV/JSON及审计 |
| 独立Review/Test |PASS|本地g5_review.md/g5_test.md；披露语义Oracle限制 |
| GitHub可审查代码与证据/远端CI |BLOCKED|本地提交与标签可审查；HTTPS写认证缺失，远端CI not_run |

用户允许认证缺失时本地交付，因此技术G5通过后进入独立最终代码/实验审计，不新增Stage6功能。若把“已成功推送GitHub”或“语义判定无误差”作为最终上线条件，则这些条件尚未满足。剩余重点是Oracle有限性、M/MS成本、SQLite/demo鉴权、依赖平台与包外data、同步协作超时；详见limitations。后续新的付费实验须另行明确预算与新协议，不延续本次账本授权。
