# Phase A 候选诊断，未验收

**G-A FAIL / BLOCKED（追加第 4 轮仍有 7 个确定误判）。这些文件不是 V1.1 正式成绩。**

- `candidate_manifest.json`：候选内容哈希、状态、真实回归结果和冻结保护记录。
- `audit_summary.json` / `audit_results.csv`：360 条 V1 历史运行的只读、事后复判，保留原成绩。
- `reviewer_pass_audit.json` / `test_pass_audit.json`：两个独立 Codex 角色的固定 40 条审查；合计 78 条不同运行，不是人类盲审。
- `expanded_pass_findings.json`：固定样本之外扩查发现的中间审批阈值误述。
- `phase_a_blockers.json`：三个合成失败正例和等价对照，以及对应函数和历史证据索引。

全部诊断须结合 [Phase A 报告](../../docs/reports/v1_1_a_oracle_ci.md) 阅读。R4 的 26 条原通过降分为自动语义范围收窄产生的待审，不能称为 26 条模型错误；详见 r4_history_review.json。V1 原始分数和数据未改变。本轮新增付费请求为 0，B–E 未执行。
