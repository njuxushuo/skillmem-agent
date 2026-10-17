# Phase 0 Report — SkillMem Agent V1

日期：2026-10-17（Asia/Shanghai）。阶段：**Stage 0，仅冻结上游与环境 / 基线盘点**。

## 目标与实际完成

**G0：PASS。** 原24个pytest全部通过、原11个离线场景全部通过；环境精确锁定并完成离线安装验证；业务代码与固定上游一致；未发现提交内容中的实际凭据；B0可由Git祖先、标签与文件哈希追溯。

- 已完整阅读用户提供的实施方案和架构规范，分别原文纳入 `docs/implementation_plan.md` 与 `docs/architecture.md`。
- 当前工作目录成为独立的SkillMem开发仓库，继承固定上游历史；`research/main-repo`与参考研究快照均未修改、未纳入Git。
- 原 `src/`、`tests/`、`data/`、`examples/`、`LICENSE`、`pyproject.toml`共22个文件逐字节保留。
- 固定Python 3.12.9 / Linux x86_64；锁定62个第三方依赖/开发/构建工具的版本与wheel SHA-256；原项目以editable、no-deps模式安装。
- 新增禁网验证入口、可公开的B0清单与本阶段摘要；更新README、CI和忽略规则。
- 完成本地Git提交。目标origin已明确，但推送预检缺少认证，因此**未推送**，没有远端提交链接。

**本阶段明确不做：** DeepSeek接入、业务修复、Skill Routing、Memory、Context、Planning、新任务集/Oracle/trace基础设施。真实API评测为 `not_run`。不创建未来阶段空模块。

## Git与源码

| 项目 | 值 |
|---|---|
| GitHub目标仓库 | https://github.com/njuxushuo/skillmem-agent |
| 本地目录 | `/data2/xs/skillmem-agent` |
| Branch | `main` |
| 原始上游 | https://github.com/nitanandsinghupw/langgraph-support-agent |
| 上游固定SHA | `b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec` |
| B0标签 | `b0-upstream`，指向原始上游提交，未增加任何适配或修复 |
| 代码/环境提交SHA | `86074ae60e6b43125e0fdef618cae0eaa6037676` |
| 代码/环境提交标题 | `chore(stage0): freeze upstream baseline and reproducible offline environment` |
| 阶段交付标签 | `stage-0`，在报告提交后创建，标识完整Stage 0交付 |
| 推送状态 | `not_pushed`：HTTPS推送预检无法读取GitHub用户名，未发现可用凭据助手、gh登录或Token环境配置 |
| 远端CI | `not_run`：本轮未推送；只能报告本地同等检查通过 |

代码/环境SHA标识本次测试对应的文件内容；之后的提交仅补充本报告与脱敏摘要。报告不嵌入自身提交SHA，以避免Git内容哈希自引用；完整交付SHA由以下命令获取，最终回复亦提供该SHA：

```bash
git rev-parse 'stage-0^{commit}'
git log -1 --format='%H %s' -- docs/reports/phase_0.md
git rev-parse 'b0-upstream^{commit}'
git merge-base --is-ancestor b0-upstream stage-0
```

Git远程区分：`origin`指向用户仓库；`upstream`指向原作者仓库。没有force push，没有将研究克隆变成子模块或嵌套Git提交。

### 修改/新增文件与来源

| 文件 | 相对B0状态 | 内容来源与原因 |
|---|---|---|
| `README.md` | 修改 | 本轮写明SkillMem名称、上游来源、Stage0真实状态、安装/测试命令、目录和限制；不声称从零自研。 |
| `.gitignore` | 修改 | 忽略凭据、`.env`、DB/WAL、日志、原始JSONL、`.local`、环境、缓存、研究克隆和原工作笔记；允许安全`.env.example`。 |
| `.env.example` | 修改 | 只列上游当前有效配置及空凭据占位；默认offline，明确不自动加载、不支持DeepSeek/SKILLMEM配置。 |
| `.github/workflows/ci.yml` | 修改 | 固定Python3.12.9/ubuntu22.04，哈希锁安装，运行禁网原回归与lint。 |
| `.python-version` | 新增 | 精确声明本次验证解释器版本。 |
| `requirements.lock` | 新增 | 62个精确版本与目标平台wheel哈希，包括原dev/openai extras及coverage/build工具。 |
| `docs/architecture.md` | 新增 | 用户批准的架构规范原文；文中目标图并不代表Stage0已实现。 |
| `docs/implementation_plan.md` | 新增 | 用户实施规范原文，保留阶段顺序与Gate。 |
| `docs/upstream_README.md` | 新增 | 固定上游README原文备份，保留出处；其中原相对链接以原仓库根目录解释。 |
| `docs/reports/phase_0.md` | 新增 | 本报告与目录审计。 |
| `artifacts/b0_manifest.json` | 新增 | 上游commit/tree、26个原文件的SHA-256/行数、源码规模。仅摘要，无用户会话。 |
| `artifacts/stage_0_summary.json` | 新增 | 脱敏测试/覆盖率/环境/数据哈希/已验证与not_run状态。 |
| `scripts/run_offline.py` | 新增 | 必要的可复现验证入口；不是Agent业务模块或Stage1评测器。 |
| `src/`、`tests/`、`data/`、`examples/`、`LICENSE`、`pyproject.toml` | 不变 | 全部来自固定上游；AgentLoop、8工具、Policy、Approval、测试均原样。 |

新Python文件仅54行：

- `configure_offline()`：无入参/返回None；强制offline、清除API_KEY/SUPPORT_DB、关闭tracing、阻止socket连接。仅依赖Python标准库。
- `main()`：读取命令行，切换仓库根目录，在防联网措施生效后以runpy执行原pytest或原evaluate模块。pytest参数原样传递；scenarios不接受provider覆盖参数。

无新业务类、无新模型调用、无循环依赖。脚本与应用是“验证入口→原程序”的单向关系，不被应用导入。

## 验证

### 环境与精确依赖

已验证平台为 **CPython 3.12.9 / Linux x86_64**，不是全平台兼容声明。系统默认Python3.8不满足上游要求，因此使用已安装的 `/home/xs/miniconda3/bin/python` 创建独立 `.venv`；未修改系统解释器，也未复用研究venv作为最终开发环境。

关键版本：

| 包 | 版本 |
|---|---|
| langgraph | 1.2.16 |
| langgraph-checkpoint-sqlite | 3.1.2 |
| langchain-core | 1.6.12 |
| langchain-openai | 1.7.2 |
| openai | 3.33.0 |
| pydantic | 2.14.1 |
| fastapi / uvicorn | 0.143.3 / 0.54.0 |
| pytest / pytest-cov / coverage | 9.1.1 / 7.1.0 / 7.16.2 |
| ruff | 0.17.1 |
| pip / setuptools / wheel | 24.3.1 / 80.9.0 / 0.45.1 |

全部62项见 `requirements.lock`。实测安装后的62项版本全部匹配，额外只有本项目editable分发。保留原openai extra只是为了冻结既有可选依赖，不表示已开发DeepSeek或调用模型API。

依赖下载阶段访问软件包源，不访问模型API；**安装和测试阶段离线**。本地wheel缓存不进Git，可离线搬运到相同平台。初次在新机器复现时需先下载缓存：

```bash
python3.12 -m venv .venv  # 先确认该解释器为3.12.9
.venv/bin/python -m pip download --only-binary=:all: --require-hashes \
  -r requirements.lock -d .local/wheelhouse
```

本次实际离线安装命令：

```bash
/home/xs/miniconda3/bin/python -m venv .venv
PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/python -m pip install \
  --no-index --find-links=.local/wheelhouse --require-hashes -r requirements.lock
PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/python -m pip install \
  --no-build-isolation --no-deps -e '.[dev,openai]'
.venv/bin/python -m pip check
```

`pip check`真实结果：`No broken requirements found.`；离线安装成功。锁文件包含目标平台实际下载wheel的哈希，其他平台需重新生成并审查锁，不应移除哈希强行安装。

### 原测试与场景：精确命令

在仓库根目录执行：

```bash
mkdir -p .local/stage0/tmp
COVERAGE_FILE=.local/stage0/.coverage .venv/bin/python scripts/run_offline.py pytest -q \
  --basetemp=.local/stage0/tmp \
  --cov=support_agent --cov-report=term-missing \
  --cov-report=json:.local/stage0/coverage.json
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

脚本执行的是原 `pytest -q` 及 `python -m support_agent.evaluate --provider offline`逻辑，仅在导入前禁用网络/外部配置，没有替换测试或业务实现。FastAPI TestClient用进程内ASGI通信，不需要网络socket。

| 检查 | 实际结果 |
|---|---|
| 原pytest | **24 passed in 2.23s**；0 failed、0 skipped |
| 原场景 | **11/11 scenarios passed** |
| 语句覆盖率 | **507/571 = 88.79159369527146%**；显示89%，未测分支覆盖 |
| Ruff lint | `All checks passed!` |
| Ruff format | `17 files already formatted`（16个上游Python文件+1个验证脚本） |
| 防联网措施自检 | create_connection、socket.connect、socket.connect_ex共3种连接入口均被拒绝 |
| 源码/夹具/许可/配置一致性 | 22/22文件与B0逐字节一致 |
| 研究快照完整性 | 主项目研究快照26/26跟踪文件哈希保持不变；参考快照未操作 |

24个pytest包含1个对11场景的汇总断言，因此不是35个独立测试。原场景包括物流、小额退款、大额待审批、超窗、他人订单、已发货拒绝取消、未发货取消、政策、缺订单号、人工转接、提示注入。

与上一次调研比较：同样24/24、11/11、507/571覆盖；本阶段应用代码无新增、修改或删除。运行耗时不同不是性能优化结论。

### 版本、规模与数据哈希

- 原Git跟踪文件26个；原Python16文件/1303物理行。
- 应用12文件/1026行；测试4文件/277行。
- Stage0新增验证脚本1文件/54行；核心应用仍1026行，不计文档/依赖为业务代码。
- `artifacts/b0_manifest.json`记录全部原文件哈希与原Git tree；完整历史中保留B0原版本。

| 文件 | SHA-256 |
|---|---|
| `requirements.lock` | `fb98eda0d02b67d4276a6e820e0b1b3046bdf00e9cd38bc4e02fca893ee4770e` |
| `data/store.json` | `246f50b7ad509a1a96046676ca9fe643a40a63f11a1b2c8d20fdf725523147de` |
| `data/policies.md` | `a39d4544e6a5a1c8eb6da3c8ce1ed190d81e66f4e4509a8192781b3ac273f6bf` |
| `data/scenarios.json` | `cf95836023bf1b195067c1941f8c52b7997f1374cac0951810abc4ad2aade2c3` |

可核验命令：

```bash
git diff --exit-code b0-upstream stage-0 -- src tests data examples LICENSE pyproject.toml
sha256sum requirements.lock data/store.json data/policies.md data/scenarios.json
git ls-files
```

### 提交安全检查与证据留存

对全部待提交文件以及原上游26文件检查API Key/GitHub Token/AWS访问键/私钥头模式，结果0命中；复核`.env.example`只含空凭据占位和配置说明。检查Git跟踪清单：不含research、venv、`.local`、数据库、原始日志或JSONL。

用11个示例路径验证`.gitignore`，涵盖`.env`、`.env.local`、`.local`日志/DB、普通DB/WAL、key、credentials、log、JSONL及研究源码，均忽略；`.env.example`通过否定规则明确放行。扫描只是可识别模式检查，不宣称是通用数据防泄漏证明。本轮未把用户此前提供的Key写入文件。

本地取证文件（忽略、不推送）：

- `.local/stage0/pytest.log`、`scenarios.log`、`coverage.json`。
- `.local/stage0/install.log`、`install_project.log`、`download.log`。
- `.local/stage0/audit.json`、`research_before.json`。
- `.local/wheelhouse/`、`.venv/`。

可公开摘要：`artifacts/b0_manifest.json`、`artifacts/stage_0_summary.json`。不包含原始客户对话或真实模型响应。

### 本阶段遇到的问题与处理

1. 系统Python3.8不符合要求：使用已有Python3.12.9创建新环境，未改业务代码。
2. 初次忽略规则探针把旧Git对`!.env.example`的“规则命中”退出码误判为被忽略：查看verbose结果确认否定规则正确，修正探针解释；不是业务测试失败。
3. 用户规范原文包含Markdown的两个行尾空格：普通`git diff --check`会提示trailing whitespace。为保持批准文档原文，使用 `git -c core.whitespace=-blank-at-eol diff --cached --check`检查其他空白错误；没有改写规范。
4. 推送预检失败：`fatal: could not read Username for 'https://github.com': No such device or address`。目标仓库已配置且读取成功、未返回refs，但HTTPS写入认证不可用。没有假报推送成功或远端CI结果。

## 实验（真实模型 not_run）

- 变体：**B0原始控制/业务逻辑**；Stage0没有加DeepSeek适配或共同仪表，不能称为后续B0-compatible在线评测已完成。
- Provider：`offline`；模型：上游`OfflineSupportModel`规则替身；fixture日期2025-10-01。
- 原单轮场景N=11，独立场景命令repeat=1；pytest中也包含该11场景的汇总检查。
- 真实DeepSeek provider/model/config：`not_run`；API请求数0，无付费调用。
- 真实Task Success、SOP合规率、Token、成本、Tool实际执行统计、端到端时延：**not_run / null**，不以offline结果或0值替代。
- 原`tools_used`只是模型提议序列，仍保留其已知限制；本阶段不提前构建Stage1评测基础设施。

## Architecture & Directory Audit

### Current tracked tree

以下由本阶段Git跟踪清单整理；最终交付35个文件。`[M]`表示相对上游修改，`[A]`表示新增；未标记为原样复用。

```text
skillmem-agent/
├── .env.example                         [M]
├── .gitignore                           [M]
├── .python-version                      [A]
├── .github/workflows/ci.yml              [M]
├── README.md                            [M]
├── LICENSE
├── pyproject.toml
├── requirements.lock                    [A]
├── artifacts/
│   ├── b0_manifest.json                 [A]
│   └── stage_0_summary.json             [A]
├── data/
│   ├── policies.md
│   ├── scenarios.json
│   └── store.json
├── docs/
│   ├── architecture.md                  [A]
│   ├── implementation_plan.md           [A]
│   ├── upstream_README.md               [A]
│   └── reports/phase_0.md               [A]
├── examples/scenarios.txt
├── scripts/run_offline.py               [A]
├── src/support_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── api.py
│   ├── evaluate.py
│   ├── graph.py
│   ├── models.py
│   ├── offline_model.py
│   ├── policies_kb.py
│   ├── policy.py
│   ├── service.py
│   ├── store.py
│   └── tools.py
└── tests/
    ├── conftest.py
    ├── test_api.py
    ├── test_conversations.py
    └── test_policy_and_tools.py
```

### 职责、状态和控制流审查

- 唯一应用包仍为`src/support_agent`；没有重复的skillmem_agent包、分层框架、Java移植或空目录。
- 原回归测试原位保留；原data只读作为固定fixture，没有写回运行状态。
- 原图仍为 `START → agent → tools → agent`、无tool_calls则END；退款仍使用原interrupt/resume。
- State schema diff：**无**，仍MessagesState；迁移：不需要。
- Graph nodes/edges diff：**无**；未新增Guard/Observe/Skill/Context等后续阶段能力。
- Module dependencies diff：应用依赖方向不变；新增验证脚本仅通过runpy调用原入口。
- Baseline behavior preserved：22个文件哈希一致 + 原24测试/11场景通过；不是仅凭README判断。
- Removed files：**无**。原用户规范和研究文件保留本地，规范的维护副本在docs，未删除用户原件。

### 与目标目录的差异及理由

1. 保留上游`examples/`与`.github/`：它们已有实际用途，不是新框架或空模块。
2. 新增`scripts/run_offline.py`：目标示意树未列scripts，但实施规范要求可复现/离线脚本；统一CLI与CI的禁网验证属于Stage0环境工作，不改变业务接口、依赖或阶段范围。
3. `artifacts/`在Stage0已有两个实际生成的清单/摘要，遵循“产生后才创建”和仅脱敏统计要求；不是提前创建Stage5实验结果。
4. `docs/implementation_plan.md`与`docs/upstream_README.md`用于规范留存和上游署名；没有创建后续evaluation_protocol/limitations空文档。
5. 原根目录研究文件、规划笔记及research仍物理存在，但被忽略，Git交付中没有重复研究克隆；不为目录整洁破坏取证快照。
6. CI从原Python3.11/3.12矩阵缩为本次精确锁定的3.12.9/Linux目标；仅对已验证环境负责，3.11及其他平台未验证。

无改变已批准业务架构、依赖边界或阶段顺序的偏差。目标架构图中的未来模块明确仍为计划内容。

## 风险与下阶段阻碍

保留为B0事实、未在Stage0修复：

- 业务订单/退款/工单在内存，SQLite只保存对话/归属/待审批；不能宣称业务跨重启可靠。
- `resolve_approval()`先删pending再resume，异常可能丢审批入口；同thread并发和多interrupt不具备完整保障。
- 40条消息裁剪不是真Token预算；无TaskMemory、Skill或独立Planning。
- `OfflineSupportModel`为英文规则模型；不能证明中文、多轮指代或真实LLM性能。政策关键词检索不支持中文分词。
- HTTP身份header及员工共享Key仅是demo鉴权。
- 原evaluator仅打印失败、不返回非零退出码；现有pytest汇总场景断言承担本阶段门禁；实际执行统计/真实oracle留给后续阶段。
- 原wheel缺包外data；本阶段使用editable源码方式，未修包分发。
- 仅CPython3.12.9/Linux x86_64依赖锁已验证；平台迁移与依赖升级须重测。
- GitHub认证缺失，远程未推送、CI未执行。已配置目标地址，无需另建或提供新仓库；只需在环境中建立该仓库的有效写入认证，勿在聊天粘贴Token。

认证完成后可由用户或后续明确授权的操作执行：

```bash
git push -u origin main
git push origin refs/tags/b0-upstream refs/tags/stage-0
```

若远端后来出现其他提交，应先fetch审查，不能强推覆盖。

### Gate判定

| G0项 | 判定 |
|---|---|
| 原24个pytest全部通过 | PASS |
| 原11个offline场景全部通过 | PASS |
| 可复现环境、精确依赖与离线命令 | PASS（明确限定已验证平台） |
| 无实际凭据/DB/原始日志提交 | PASS（扫描及跟踪清单检查） |
| B0源码/许可证/上游来源可追溯 | PASS |
| 阶段报告与架构审计 | PASS |
| Git版本记录 | PASS（本地）；GitHub发布受认证阻碍，未伪造成功 |

**进入Stage1的前置条件：用户审查并明确确认本阶段。** 本轮结束即停止；本地Stage1开发不依赖推送权限，但远端交付需先配置认证。Stage1真实API smoke另需有效环境凭据及明确预算，本轮不使用此前贴出的Key。DeepSeek、Skill、Memory、Context或Planning均未提前实现。
