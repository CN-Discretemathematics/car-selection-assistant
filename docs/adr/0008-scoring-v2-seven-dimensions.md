# 评分 v2：七维、固定锚点、安全配置独立成维

**状态**：accepted（2026-10-27 用户拍板；方案见
[评分 v2 设计稿](../2026-10-27-scoring-v2-design.md)）

## 问题

v1 的 8 维评分（budget / usage / space / energy / power / comfort / intelligence / maintenance）
有五个结构性问题，且已被生产数据验证：

1. **口径名不副实**：energy 维不读油耗/电耗，只要有 energy_preference 就恒 1.0；
   budget 维在「只给上限」时因 SQL 已按预算过滤而恒 1.0（tools.py:705-714）。
2. **批内归一化**：comfort/intelligence 按本批候选最大值归一（tools.py:831-856），
   同一款车换一个候选池分数就变，与「确定性、可复算」冲突。
3. **缺数据语义不一致**：space/power 缺失记 0.5，comfort/intelligence 缺失记 0.0 且仍占权重。
4. **同车系同质化**：8 维全部是车系级或布尔，生产逸动 3 个款型得分逐位相同（0.8816）；
   2026-10-24 改为标配项数后仍只有 comfort/intel 能区分。
5. **安全没有维度**：weights.py:122-152 明确记录「安全/碰撞/气囊刻意不映射任何维度」，
   而用户把「最看重安全」说出来时只能落空。

## 考虑过的选项

1. **保留旧 8 键、只做小修**：不采纳。budget 恒 1.0、energy 不读能耗、maintenance 无数据，
   是语义错误，不是参数调优能解决的。
2. **学习型权重 / LTR（pairwise logistic）**：推迟。需要至少约 1500 对人工成对标签，
   当前没有，先固定锚点 + 可解释权重。
3. **LLM 排序 / rerank**：不做。违反 ADR-0002（不做 agentic RAG），且破坏确定性与可复算。
4. **七维 + 固定锚点 + 同价位比较**：采纳。

## 决定

- 维度 7 个：usage / space / running_cost / power / safety / comfort / intelligence。
  预算、能源偏好、品牌、车身类型只做**硬约束**，不再参与打分。
- 权重：safety 0.22、running_cost 0.18、space 0.15、power 0.13、comfort 0.12、
  intelligence 0.10、usage 0.10；usage 未激活时移除并归一；用户强调 +0.20 后归一。
- **全部七维按同价位比较**（2026-10-27 01:19 用户确认）：按 10 个万元价格带分别生成
  p10/p50/p90 锚点；带内样本 n<30 回退全库。锚点写入版本化 JSON
  （backend/app/agent/scoring_anchors_v2.json），不在请求内统计。
- 缺数据统一 û=0.5，返回 coverage = 已测权重 / 激活权重；coverage<0.5 标 low_evidence；
  U = Σw·û / Σw。分数与候选池无关，可复算。
- 安全 = 「安全配置」类目下 10 组关键配置的 ● 命中组数，只称**安全配置水平**，
  不承诺碰撞星级、不宣称「最安全」。
- 排序：U → coverage → 最新销量 → variant_id；多样性同车系≤2、前 5≥3 车系。
- 开关：AGENT_SCORING_MODE=off|shadow|new（默认 off）；SCORING_VERSION=scoring-v2；
  无数据库迁移，回滚改环境变量。

## 后果

1. /recommendations 的 weights_used 键集从 8 键变为 7 键，新增 scoring_version /
   anchor_version / diversity_relaxed / usage_active；属破坏性契约变更，当前无已知外部消费者。
2. 旧 8 维测试、soft_prefs 词表（SOFT_PREF_VERSION 递增到 soft-pref-v3）、文案与注释需同步；
   安全类强调从「不可映射」改为映射 safety。
3. 安全只是配置代理，文案与测试名必须显式写「安全配置水平，非碰撞安全」。
4. 锚点随目录漂移：anchor_version 版本化 + 重跑脚本 + p50 漂移 >10% 告警。
5. shadow 模式与 v1 并行只为观察差异，不计入用户结果；v1 保留至少一个发布周期。

## 参考

- [评分 v2 设计稿](../2026-10-27-scoring-v2-design.md)
- [2026-10-24 款型级打分修复](../2026-10-24-variant-level-scoring.md)（已被本决策取代）
- 数据事实：backend/app/agent/scoring_anchors_v2.json 的生成来源（2026-10-03 生产快照）
