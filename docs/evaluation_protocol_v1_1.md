# V1.1 候选评测协议：Phase A 阻断快照

状态：**G-A FAIL / BLOCKED，未经接受，不能用于正式新成绩。** 方案见
[optimization_plan_v1_1.md](optimization_plan_v1_1.md)。本文件补充 V1 协议，不替换
`evaluation_protocol.md`、V1 Oracle、任务、成绩或冻结 manifest。

## 当前实现与边界

`oracle_v2.judge_v2`接收任务、前后业务状态、最终回复、完成标志、审批与原始事件，分别输出：

- `business_success`：目标业务结果及未授权副作用检查。
- `hard_safety_pass`、`soft_sop_pass`、`safety_sop_pass`：硬约束与推荐顺序分离；不适用项为 null，不计入适用分母。
- `response_facts_pass`：关键事实、拒绝理由及中间动作声明是否有证据。
- `completion_pass` 与 `full_task_success`：按任务适用规则联合判定；保留逐规则原因及 `needs_review`。

候选标识为 `2.0.0`，组合内容哈希见 `artifacts/v1_1/candidate_manifest.json`，**不是已验收发布版本**。内容哈希同时覆盖 V2 与其依赖的 V1 evaluation、policy、store。正则模式仅支持受约束语义，不能作为通用自然语言真实性证明。当前有三个确定误拒，详见阶段报告和阻断测试。

## 只读历史复判

```bash
.venv/bin/python scripts/audit_v1_results.py
```

脚本禁网，不运行模型/业务任务；校验原结果、原始 trace、任务/会话/变体/模型配置/来源哈希，重算 V1 判分核对原成绩，再生成 V2 诊断。默认读取本地忽略目录 `.local/stage5/final_results.json`，输出独立 `artifacts/v1_1/audit_summary.json` 与 CSV。原始轨迹缺失时保持 uncovered/null 并非零分或成功；未持有私有原始证据的外部读者只能复算公开 CSV 汇总，不能独立重演完整语义判断。

保留 `v1_frozen_score` 与 `oracle_v2_diagnostic_score`，样本单位为一次运行；360 次运行来自 30 个任务 × 4 变体 × 3 次重复，不是 360 个独立任务。旧 13 个确认误拒与 8 个争议单列。原通过池分层审查分两个固定 40 条样本，共 78 条不同运行；属于独立 Codex 角色审阅，不是人类盲审，不外推全体误放率。

公开工件仅含脱敏的规则、分数、hash、case/run/step/tool-call 标识。完整请求、回复、业务快照、凭据和数据库保持本地忽略。候选诊断必须与同目录 `candidate_manifest.json` / `phase_a_blockers.json` 一起解释，不能宣传为正式修正成绩。

## 后续实验状态

B–E 全部 `not_run`。尚未冻结新 Dev/Holdout、M-opt/MS-opt 或因素配置；未形成成本归因或优化收益结论。V1 的真实 Provider Usage 保持原记录，本阶段没有新增 Provider Usage、模型请求或费用。

只有重新获准修复 Phase A 并通过 Gate 才能继续后续阶段。未来真实 Dev/Holdout 必须单独取得请求次数及金额上限；旧授权不复用。当前既不是 `EVAL_READY`，也不是 `G-E_REAL_LLM_PASS`。
