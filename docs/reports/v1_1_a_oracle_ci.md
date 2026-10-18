# V1.1 Phase A：Oracle V2 与 Git/CI 阻断报告

日期：2026-10-18（Asia/Shanghai）。**G-A FAIL / BLOCKED。** 第三轮独立审查仍发现确定评分误判，已按批准方案 §8–9 的三轮上限停止实现。没有进入 Phase B–E，不是 EVAL_READY。新增付费 API 请求 **0**。

## 1. 目标与实际完成

已建立与 V1 分离的候选 Oracle V2、只读历史审计脚本、正负例回归与公开脱敏证据；完整复判 360 条历史运行，并由独立 Code Review / Testing 两个角色审阅原通过样本。原 13 个已确认误拒得到修正，8 个争议保持待审。然而候选新增了对合法零退款和否定审批句的误拒，不能冻结为通过版本。

已纳入用户完整方案 `docs/optimization_plan_v1_1.md`，与原文件逐字节一致。根目录原文件仍保留、通过 `.gitignore` 忽略，避免重复文档。没有修改 Agent、Prompt、业务规则、审批、Memory/Context/Skill 或 V1 Oracle；没有执行新付费请求、读取 Key 或构建新 Holdout。

## 2. Git 与可追溯性

- Repository：<https://github.com/njuxushuo/skillmem-agent>。
- V1 最终代码、起始 HEAD、本地 main、获取后的 origin/main 均为 `a34d5dab42100dc03820453976edd5940e0afd54`。旧报告所述 Stage 3–5 尚未上远端的状态已过时；本次获取确认 V1 最终提交已存在远端。
- 当前交付分支：`v1.1/phase-a-blocked`，避免把包含明确失败测试的候选直接放入稳定 main。
- V1 实验代码：`c63ec4d189830be94e30c9fc6dd3416ee6e9b97c`；stage-3/4/5 解析后的提交分别为 `175b4a4ca60255f44c3689cbaf7acf7c5cec0a42`、`dee24d95b1b4071428951d3e7dfc05bfc506585a`、`c3a0a5a8711b307ad859b3fef020a9704957b21a`。
- 候选代码提交：见本报告末尾交付记录（提交后追加），或 `git log --all --format='%H %s' --grep='preserve blocked Phase A'`。报告提交不包含行为变更。
- 不创建 `v1.1-a` 验收 Tag：用户规定通过 Gate 后创建 Tag，本阶段未通过；原 10 个 Tag 均保持原对象。
- 正常 HTTPS 推送遇到 `could not read Username for 'https://github.com': terminal prompts disabled`；没有强推、覆盖历史或绕过认证。
- 只读 Actions 查询起始 HEAD 得到 HTTP 403，无法确认最新 V1 CI；新候选 CI 为 `not_run`，不冒称成功。即使远端认证恢复，当前三项测试也会阻止 CI 通过。

## 3. 文件、职责与调用链

| 文件 | 关键接口与职责 |
|---|---|
| `src/support_agent/oracle_v2.py` | `judge_v2`、`oracle_metadata`；纯函数候选评分，拆分业务、安全/SOP、回复及完整成功，按 action.result 检查中间声明 |
| `scripts/audit_v1_results.py` | `read_evidence` 校验证据关联；`audit` 重算原评分与 V2；`write_outputs` 导出白名单字段；`main` 禁网与独立输出目录 |
| `tests/test_oracle_v2.py` | 实现者的 82 个受约束正负例测试 |
| `tests/test_oracle_v2_acceptance.py` | 独立 Testing 角色的 23 个验收测试，含最终 3 个显式失败断言 |
| `tests/test_v1_audit.py` | 11 项：确定性、缺失/损坏/错配证据、变体/来源/模型关联、错误消息隐私 |
| `artifacts/v1_1/` | 脱敏复判、独立审查、阻断复现与候选 hash，不包含原始 trace |
| `docs/evaluation_protocol_v1_1.md` | 候选协议与后验结果边界，不覆盖 V1 协议 |
| `README.md`、本报告及最终审查 | 当前阻断状态、可复现命令与交付说明 |

调用链：离线审计 CLI → 读取/校验历史 evidence → `evaluation.judge` 核对 V1 → `oracle_v2.judge_v2` → JSON/CSV。不调用 Service/Graph 执行业务；测试所需模拟业务遵循原 Runner 的独立 Store/临时数据库/Thread。

候选 `2.0.0` 的组合 hash 为 `e13e7619bfb6d34e75aad1e5567336ed9dada3fff10546d48d1c68389adb1234`，源码 hash 为 `b37f97396eeb96c5314aa9fee70449e192351515ad6a5f0bb12e6b7f29115480`。版本号仅识别候选，不表示接受；细项哈希见 `artifacts/v1_1/candidate_manifest.json`。

## 4. 测试、审查与失败修复过程

角色分离真实执行：engineering 实现，architecture 独立代码审查，reference 独立测试和语义审计；协调者集成及回归。不是人类盲评，也不是仅由实现者自审。

| 轮次 | 证据与结果 | 处理 |
|---|---|---|
| R1 | 独立全量 633 passed / 10 failed；假完成、金额/日期诱饵、政策对象/单位、safe_stop、中间泄露等反例失败 | 修复受限语义匹配；审计脚本修复 ValidationError 原输入泄露及变体关联不足 |
| R2 | 当时全量 657 passed；独立扩展验收 15 passed / 5 failed | 修复 zero/nothing 否定、自动政策批准与人工批准区分、千位/裸金额；不得因旧测试绿而忽略新反例 |
| R3 | 既有重点测试 113 passed；新增三个通用正例全部失败 | 保留失败测试及证据，停止实现，没有第四轮修复 |
| 最终全量 | **677 passed / 3 failed，28.04 秒** | 失败均为 R3 的三参数验收测试；未使用 xfail/skip 弱化 |

总计 680 项 = 原 564 项 + 新增 116 项；新增 113 通过 / 3 失败。最终测试后只对独立测试文件做 Ruff 排版，不改变断言或评分逻辑；排版后单独重跑独立验收文件仍为 20 通过 / 3 失败（0.12 秒）。

```bash
# 全程禁网、移除 API Key 环境变量并使用临时数据库
.venv/bin/python scripts/run_offline.py pytest -q
.venv/bin/python scripts/run_offline.py scenarios
.venv/bin/python scripts/run_offline.py tasks
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pip check
# 独立复现阻断
.venv/bin/python scripts/run_offline.py pytest -q tests/test_oracle_v2_acceptance.py
# 只读复判；需要本机保留的私有原始轨迹
.venv/bin/python scripts/audit_v1_results.py
```

原 **11/11 场景、7/7 任务**通过。Ruff check 通过、46 个 Python 文件格式检查通过、pip check 无依赖冲突。原 564 项测试全通过，包括既有业务安全、Tracing、Usage、Skill 和审批回归；不是新增真实 LLM 效果证明。原始测试输出在忽略目录 `.local/v1_1/a/pytest_final_blocked.log`、`scenarios_final.log`、`tasks_final.log`。三轮完整审查记录位于同目录 `review_round1.md`、`review_round2.md`、`review_round3.md` 与 `test.md`；公开最小证据在 `phase_a_blockers.json` 与提交的失败测试。

### 最终阻断：通用合成正例

失败函数：`test_declared_zero_and_negative_approval_grammar_in_formatted_replies`。

| 正确陈述 | 应有结果 | 当前错误与定位 |
|---|---|---|
| 12 EUR refund **wouldn't need human approval** | 低于 50 EUR 阈值，不要求人工；通过 | `_NEGATION` / `_statement_checks` 将 wouldn't 当肯定，误报错误审批阈值；would not 等价形式可通过 |
| **Refunded so far: 0** | 查询陈述零退款，未声称完成；通过 | `_statement_checks` 未识别无币种的零累计，误报无实际副作用的退款完成声明 |
| 分行的 **Refunded so far: 0 EUR / Maximum refundable: 18 EUR** | 零已退与最高可退为不同事实；通过 | `normalize_text` / `_claim_clauses` 合并换行，误将最高可退金额绑定到已退款 |

历史关联只用于说明影响；新增回归使用通用合成订单，不以 Final case ID 硬编码。后续最小修复需要保留声明边界、区分累计与动作/上限、统一受限否定句处理，并重新独立验收；本轮没有实施第四次修复。

## 5. 历史复判与通过样本审查

输入 360/输出 360，唯一键为 `(case_id, variant, repeat)`；30 个任务 × 4 变体 × 3 次重复。输入文件 SHA256 为 `c36460177ef4f3761c917efd6e3fa362c315757fbc876795d5a631ad6609f24b`。trace、会话、变体、模型和来源验证成功；可复算脚本保留缺失证据分支，不假设其他机器也持有原始数据。

**下表全部为未验收候选诊断，不是修正后的正式成绩，更不是新的模型实验。**

| 变体（每组 90 次） | V1 冻结完整通过 | V2 候选完整通过 | 改判次数 | 业务通过 | 回复事实通过 | 硬安全通过 |
|---|---:|---:|---:|---:|---:|---:|
| B1 | 72 | 75 | 5 | 81 | 75 | 90 |
| B1_SOP | 75 | 77 | 4 | 81 | 77 | 90 |
| M | 85 | 85 | 6 | 89 | 86 | 90 |
| MS | 78 | 81 | 3 | 86 | 84 | 90 |

13 条旧失败变为通过：12 条中文数字相邻边界、1 条等价安全拒绝。旧 8 个争议（6 条最终回复省略订单号、2 条安全拒绝缺要求的业务理由）仍标记待审且不计完整成功。5 条原通过变为失败中包含当前评分器的确定误拒，**不能称为发现 5 条旧误放**，硬安全数字也不构成通用安全证明。

原通过池 310 条。ReviewerA 和 TestB 各按变体/类别固定抽样 40 条，重叠 2 条，共 **78 条不同运行**；逐行 case/repeat/step/tool-call/hash 证据见公开两个 pass_audit JSON。固定样本未发现确定的任务级误放；ReviewerA 标记 2 条更宽的回复歧义（承诺更新工单的能力、缺金额限定的审批描述）。不能外推剩余样本安全或声称人工盲审。

针对改判扩查另发现 `f_multi_05 / M / repeat 1 / step 36` 将 40 EUR 描述为高于自动退款阈值而需要人工；实际阈值 50 EUR，之后是 policy 批准。业务结果正确，但中间解释错误。这一条**不在固定 40 样本内**。当前候选对此运行报错仍混入错误零退款绑定，不能把错误失败理由当作精准发现。见 `expanded_pass_findings.json`。

公开 CSV 汇总复算：

```bash
.venv/bin/python - <<'PY'
import csv
from collections import defaultdict
groups = defaultdict(list)
with open('artifacts/v1_1/audit_results.csv') as stream:
    for row in csv.DictReader(stream):
        groups[row['variant']].append(row)
for variant, rows in sorted(groups.items()):
    print(variant, len(rows),
          sum(r['v1_frozen_score'] == 'True' for r in rows),
          sum(r['oracle_v2_diagnostic_score'] == 'True' for r in rows))
PY
```

## 6. Architecture & Directory Audit

```text
src/support_agent/     原 19 模块不改；新增 oracle_v2.py
scripts/               原工具不改；新增 audit_v1_results.py
tests/                 原 19 文件不改；新增 3 个实际测试文件
data/                  全部保留，无新 Dev/Holdout
artifacts/v1_1/        新增候选审计/失败证据/manifest/说明
docs/                  新增 optimization_plan_v1_1 / evaluation_protocol_v1_1
docs/reports/          新增 v1_1_a_oracle_ci / v1_1_final_review
README.md / .gitignore 增量状态说明及重复输入文档忽略规则
.local/v1_1/           忽略的原始日志与独立审查工作文件
```

应用 Python 从 19 文件/4056 物理行增为 20 文件/4694 行（+638）；新增审计脚本 233 行、测试 880 行，总新增 Python 1751 行，无新依赖。实际文件列表可由候选 commit 的 `git show --stat` 核对。

相对架构 §13 “Oracle 在 evaluation.py”存在一个明确最小偏差：为保持 V1 980 行评分/运行文件逐字节不变，将候选纯评分函数放在同包 `oracle_v2.py`，单向导入旧评测/Policy/Store；脚本只编排读证据及导出，不复制业务执行、模型或新框架。没有修改 State、数据库 schema、Graph 节点/边、interrupt/resume、ToolMessage 或执行预算。新增 638 行受限规则已暴露维护复杂度，尚不能认定此实现是可靠的最终最小方案。

结束前核验 30 个保护文件、10 个 Tag 及原 Final 输入 hash 不变；独立 Testing 另核验原 manifest 的 54 个既有文件和 36 个历史公开工件/报告/许可证均不变。新增文件不写回 V1 冻结 manifest。原 MIT 与来源说明保留。候选变更扫描未发现 Key/私钥模式；公开 JSON 无原始轨迹路径、绝对本地路径、模型请求或业务前后快照字段；Git 未跟踪 .env、.local 或数据库。扫描仅证明已执行的模式/字段检查，不代替无限制的隐私证明。

## 7. G-A 逐项判定

| 验收项 | 结果 | 证据 |
|---|---|---|
| 版本/hash 与三层独立复算 | 工程实现具备；接受冻结 FAIL | candidate_manifest / audit_summary；候选 2.0.0 未接受 |
| 已知误判回归及正负例全部通过，原 V1 不变 | **FAIL** | 13 旧误拒修正、8 争议保留、V1 不变；但最终 3 个正例失败 |
| 360 条输入输出可复算、覆盖真实 | PASS | audit_v1_results.py；360/360，源 hash、身份及来源校验 |
| 原有全量 pytest、11 场景、7 任务 | PASS（仅原回归） | 原 564 通过；11/11；7/7；候选全量 677 通过/3 失败 |
| 通过样本审计不少于 40 | PASS | 两组固定 40，78 distinct；不是人工盲审 |
| 最新远端 CI 成功 | **REMOTE_PENDING / not_run** | 写认证缺失、Actions HTTP 403，未验证成功 |
| 本阶段总体与继续资格 | **FAIL / BLOCKED** | 三轮上限已到，不能以 REMOTE_PENDING 代替本地语义失败 |

## 8. 遗留与后续条件

需用户批准额外 Phase A 修复/独立审查轮次，才能修正三个边界并重开 Gate；不需要为这项离线修复申请付费预算。必须保持已有失败断言与负例，再核验 360 条变化原因，避免扩大误放。远端需配置正常 GitHub 写认证，并在候选测试全部通过后验证确切提交 CI。

Phase B 成本归因、C 因素分解/Memory 优化、D 路由、E 新任务与冻结全部 **not_run**。TaskMemory 与历史保留的独立收益、Token 增长来源、MS 退步归因及优化效果本轮均未验证。不得用 A 的后验分数补足这些结论，也不得称已完成离线 EVAL_READY。

## 9. 最终保存与推送记录

候选源码、全部失败断言、脱敏审计和阶段报告已保存在本地提交 **`2ec2e2fef5d8691cbb040033c1a7f0fb5d710c5b`**，标题 `wip(v1.1): preserve blocked Phase A oracle candidate and audit evidence`。它是失败候选快照，不是验收通过提交。随后只追加交付信息与独立最终报告；最新文档提交可用 `git log -1` 获取，避免报告自引用 SHA。

向 `origin` 的 `v1.1/phase-a-blocked` 正常推送再次因 HTTPS 用户认证缺失失败，证据 `.local/v1_1/a/push_candidate.log`；没有远端候选 Commit URL 或对应成功 CI 可提供。分支仅本地，main 仍在 V1 最终版本，未创建通过 Tag。源码/测试/脚本空白检查通过；完整 diff 的空白提示来自 CSV 标准 CRLF 与原样方案 Markdown 的显式双空格换行，为保持输出及输入 hash 未修改这些字节。
