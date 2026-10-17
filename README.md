# SkillMem Agent

基于 [nitanandsinghupw/langgraph-support-agent](https://github.com/nitanandsinghupw/langgraph-support-agent) 的轻量单 Agent 电商售后研究项目，沿用 LangGraph、原8个业务工具、政策校验和人工审批。

**当前完成 Stage 4：动态 Skill（G3/G4 PASS）。** 新增 M 变体，B1 的 SQLite 幂等、审批恢复和执行限制共用；B0/B1 独立保留。354项离线测试通过；详见[Stage 4报告](docs/reports/phase_4.md)，真实模型消融尚未运行。


## 来源与版本

- 固定上游：[`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`](https://github.com/nitanandsinghupw/langgraph-support-agent/commit/b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec)，本仓库 Git 历史保留该提交。
- `b0-upstream` 标签：未经修改的上游原版本；不含后续 B1 修复或 Stage 1 仪表。
- `stage-0` 标签：本阶段可复现开发环境与报告。标签在验收后创建。
- 保留原作者 Nitanand Singh 的 [MIT LICENSE](LICENSE)；[上游 README 原文](docs/upstream_README.md)仅作历史留存，其中相对链接以原仓库根目录为准。
- 参考 [OWWZO/ai-agent](https://github.com/OWWZO/ai-agent) 的设计思想，不包含其 Java 源码。

## 安装：已验证目标环境

固定为 **CPython 3.12.9 / Linux x86_64**，见 [.python-version](.python-version)。`requirements.lock` 锁定运行依赖、原 dev/openai extras、覆盖率及构建工具的精确版本与 wheel SHA-256；不是跨平台锁。

先确认 `python3.12 --version` 为 3.12.9。初次下载依赖需要访问软件包源，但不访问模型服务、不需要 API Key：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip download --only-binary=:all: --require-hashes \
  -r requirements.lock -d .local/wheelhouse
.venv/bin/python -m pip install --no-index --find-links=.local/wheelhouse \
  --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps -e '.[dev,openai]'
.venv/bin/python -m pip check
```

后续在相同平台保留或离线搬运 `.local/wheelhouse`，即可跳过下载步骤完全离线重建环境。wheelhouse、虚拟环境和日志不提交。其他Python版本/平台需重新验证依赖锁，不能直接宣称通过。

采用源码 editable 安装是因为上游发行包没有正确包含包外 `data/`。本阶段记录此问题，不改动业务源码/打包行为。

## 离线验证

```bash
.venv/bin/python scripts/run_offline.py pytest -q \
  --cov=support_agent --cov-report=term-missing
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

校验入口强制 `offline`、移除 API Key 环境项、关闭外部 tracing、禁止 Python socket 连接，然后运行原始 `pytest` 和 `python -m support_agent.evaluate --provider offline`；没有更改原测试或场景逻辑。场景与任务评测失败均返回非零退出码。

原24测试和11场景保留，新增 DeepSeek Mock、Tracing、Oracle/隔离测试及7个开发评测任务。原24测试中有1个汇总运行11场景，不能重复计为35个独立测试。Stage 0 证据见 [phase_0](docs/reports/phase_0.md)，Stage 1 实际结果见 [phase_1](docs/reports/phase_1.md)。规则模型与 Mock 不代表真实 LLM 成功率。

## 本地演示

```bash
SUPPORT_MODEL_PROVIDER=offline .venv/bin/python -m support_agent --customer C100
```

`.env.example`只说明配置；程序不会自动加载`.env`。CLI/API均读取`SUPPORT_DB`，默认`.local/db/support.sqlite`并创建父目录；B1保存业务、审批、checkpoint与执行计数。直接构造`SupportService(db_path=None)`仅用于临时运行，没有跨进程持久化。演示业务日期固定为2025-10-01。

API的`X-Customer-Id`和员工共享Key只是演示鉴权，不可直接对外部署。B1已验证本机SQLite持久化、异常恢复和串行执行；B0保留原有风险。文件锁限定本机POSIX文件系统，工具截止时间为协作检查，不声称生产支付或分布式可靠性。见[limitations](docs/limitations.md)。

## 当前目录与职责

```text
skillmem-agent/
├── src/support_agent/         # 唯一应用包；含 evaluation/tracing/execution
├── tests/                     # 原测试保留；新增 persistence/reliability 异常测试
├── data/                      # 原数据不变；新增 eval_tasks.json 开发夹具
├── examples/                  # 原客服输入示例
├── docs/
│   ├── architecture.md        # 批准的目标架构，后续模块尚未创建
│   ├── implementation_plan.md # Stage 0–5实施规范
│   ├── upstream_README.md     # 原作者README留存
│   ├── evaluation_protocol.md # 当前测量口径与复现方式
│   └── reports/phase_{0,1,2}.md # 阶段结果和Architecture & Directory Audit（含phase_2）
├── artifacts/                 # B0清单、阶段脱敏统计；无完整原始日志
├── scripts/run_offline.py     # 本地/CI共用的禁网验证入口
├── .github/workflows/ci.yml   # 固定环境的离线回归
├── .env.example、.gitignore、.python-version
└── README.md、LICENSE、pyproject.toml、requirements.lock
```

本地`research/`取证快照及旧笔记不提交；`.local/`保存依赖缓存、原始日志和运行数据库。Stage 3新增`state.py/memory.py/context.py`，均有实际调用与测试；新增`skills.py`与`data/skills/`四份SOP；Planning未实现。

[实施规范](docs/implementation_plan.md)决定功能范围和Gate；[架构规范](docs/architecture.md)决定目录和依赖边界。本轮经授权，Stage 3–5依次通过独立审查与测试后自动推进。


## Stage 1 测量与 DeepSeek

新任务入口：

```bash
.venv/bin/python -m support_agent.evaluate --provider offline \
  --tasks data/eval_tasks.json --repeat 2 --output .local/runs
```

每个任务使用独立 Store、临时 SQLite 与会话；输出JSONL/CSV/JSON。默认变体为B1；通过`--variant B0-compatible`选择原行为兼容路径。B0权威版本固定在`b0-compatible`标签，B1固定在`b1-baseline`；两者使用独立数据库。以最终订单/退款状态判成功，工具提议与函数真实执行分别计数；usage缺失为null，不用0伪装。

DeepSeek使用环境变量配置，见 [.env.example](.env.example)。默认`deepseek-flash`、Chat Completions、thinking disabled、temperature=0、1024输出Token、60秒超时、不自动重试。`bind_tools`、多轮tool_call_id与审批恢复已通过Mock和真实服务端冒烟验证（仅3个合成业务任务，不代表通用成功率）。程序不会自动加载.env或运行付费Smoke。

完整原始轨迹只写Git忽略的`.local/`。当前7个任务属于开发夹具，未来独立Final集尚未冻结；SOP目前仅计量退款前资格查询。协议、计数/Token/审批语义与限制见 [evaluation_protocol.md](docs/evaluation_protocol.md)。


## B1 使用与恢复

```bash
# 默认B1：原场景、新任务，全程禁网
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks
# 明确选择变体与共同预算（只用离线模型）
.venv/bin/python -m support_agent.evaluate --provider offline --variant B1 \
  --tasks data/eval_tasks.json --max-model-calls 8 --max-tool-calls 8 \
  --turn-timeout 120 --tool-timeout 5
# 历史兼容路径；91项旧pytest也明确运行在这个模式
SKILLMEM_VARIANT=B0-compatible .venv/bin/python scripts/run_offline.py tasks
```

`B1`仍全量绑定8工具、使用原System和40条历史策略；不包含Memory/Skill收益。默认每用户轮次最多8次模型尝试、8次工具尝试；批准恢复计入原预算，等待人工的时间不计入本次执行截止时间。模型SDK重试必须为0；工具异常不自动重放。未来M/MS须共用同一执行层和预算。

审批使用`approval_id`匹配决定；重复相同决定返回已保存结果，不同决定返回409。同会话出现过多个审批后必须提供ID。中途失败保留原决定，重复同一审批决定即可恢复；不能更换批准人/备注来覆盖既定决定。非审批checkpoint故障通过服务接口`retry_turn(customer_id, thread_id)`显式恢复，不重新提交原用户消息。该维护接口尚未暴露HTTP路由。

B0数据库缺少历史业务记录，禁止直接用B1打开后重新种入订单；使用新B1数据库，或人工对账后另行迁移。CLI/APIs默认共用同一数据库，隔离演示时请配置不同路径。程序没有接入任何真实支付或订单系统。

## Stage 3 上下文配置

`SKILLMEM_VARIANT=M`启用来源可追踪的任务记忆和上下文视图。`SKILLMEM_CONTEXT_STRATEGY`支持`structured_memory`（默认）、`full_history`、`legacy_trim`。`SKILLMEM_CONTEXT_BUDGET=16384`是总估算窗口，包含工具Schema、系统规则、任务状态、历史、模型输出预留和安全余量。UTF-8字节估算是保守诊断，不是DeepSeek精确Tokenizer或真实计费用量。固定规则/最新请求放不下时明确终止；完整checkpoint历史不删除。

```bash
SKILLMEM_VARIANT=M .venv/bin/python scripts/run_offline.py tasks
```

B0/B1仍使用原历史策略；M纯上下文策略对照可显式指定`full_history`或`legacy_trim`，它们超预算时失败，不暗中进行额外裁剪。用户约束属于未验证输入，工具事实带来源和历史快照标记；业务执行仍由原Policy重验。

## Stage 4 Skill与公平对照

`SKILLMEM_VARIANT=MS`在M上启用四个Skill：order_shipping、refund、cancel、policy_support。同一个模型通过`select_skill`选择后，下一请求只绑定对应业务工具与公共选择/人工升级工具；执行Guard再次检查允许列表。Skill选择不授予业务权限，也不替代审批或Policy。

```bash
SKILLMEM_VARIANT=MS .venv/bin/python scripts/run_offline.py tasks
.venv/bin/python -m support_agent.evaluate --provider offline --tasks data/eval_tasks.json --variant B1 --full-sop
```

`--full-sop`供B1/M全量注入与MS完全相同的四份SOP，作为知识量公平对照；MS拒绝该组合。原11场景的精确工具序列只用于B0/B1/M回归，MS另计真实选择步骤。规则模型新增仅用于Skill协议演示的选择逻辑，不能据此推断真实模型效果。统计保留业务工具执行数、Skill选择数、两者合计和所有模型usage；选择模型调用的Token是总量子集，不能重复加总。
