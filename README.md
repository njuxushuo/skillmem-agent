# SkillMem Agent

基于 [nitanandsinghupw/langgraph-support-agent](https://github.com/nitanandsinghupw/langgraph-support-agent) 的轻量单 Agent 电商售后研究项目，沿用 LangGraph、原8个业务工具、政策校验和人工审批。

**当前仅完成 Stage 0：冻结 B0 原始基线与离线环境。** DeepSeek、Skill Routing、TaskMemory、Token Context Budget、Planning 和真实模型评测均尚未实现。每个阶段需要审查后才继续；规划不等于已实现功能。

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

## Stage 0 离线验证

```bash
.venv/bin/python scripts/run_offline.py pytest -q \
  --cov=support_agent --cov-report=term-missing
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

校验入口强制 `offline`、移除 API Key 环境项、关闭 tracing、禁止 Python socket 连接，然后运行原始 `pytest` 和 `python -m support_agent.evaluate --provider offline`；没有更改原测试或场景逻辑。原 evaluator 失败退出码缺陷暂时保留，原 pytest 中的场景汇总断言承担门禁。

预期 **24 个 pytest 全通过、11/11 场景通过**。24 个测试中有1个汇总运行这11个场景，因此不是35个独立测试。真实结果、覆盖率、命令及限制见 [阶段报告](docs/reports/phase_0.md)。这些是规则模型回归，不代表真实 LLM 成功率。

## 本地演示

```bash
SUPPORT_MODEL_PROVIDER=offline .venv/bin/python -m support_agent --customer C100
```

`.env.example`只说明现有配置；程序不会自动加载`.env`。`SUPPORT_DB`目前只由API读取，CLI不会读取。订单、退款、工单仍在内存；演示日期固定为2025-10-01。

API的`X-Customer-Id`和员工共享Key只是演示鉴权，不可直接对外部署。业务状态持久化、审批恢复异常、并发、多工具审批等已知限制保留为B0事实，不声称生产可靠性。

## 当前目录与职责

```text
skillmem-agent/
├── src/support_agent/         # 上游12个应用Python文件，原样复用
├── tests/                     # 上游4文件/24测试，原样复用
├── data/                      # 原商店数据、政策、11场景，原样复用
├── examples/                  # 原客服输入示例
├── docs/
│   ├── architecture.md        # 批准的目标架构，后续模块尚未创建
│   ├── implementation_plan.md # Stage 0–5实施规范
│   ├── upstream_README.md     # 原作者README留存
│   └── reports/phase_0.md     # 阶段结果和Architecture & Directory Audit
├── artifacts/                 # 仅B0清单与脱敏Stage0统计
├── scripts/run_offline.py     # 本地/CI共用的禁网验证入口
├── .github/workflows/ci.yml   # 固定环境的离线回归
├── .env.example、.gitignore、.python-version
└── README.md、LICENSE、pyproject.toml、requirements.lock
```

本地`research/`取证快照及旧笔记不提交；`.local/`保存依赖缓存、原始日志和运行数据库。没有提前创建空的`state.py/memory.py/context.py/skills.py/planning.py`。

[实施规范](docs/implementation_plan.md)决定功能范围和Gate；[架构规范](docs/architecture.md)决定目录和依赖边界。Stage 0结束后等待审查，禁止自动进入Stage 1。
