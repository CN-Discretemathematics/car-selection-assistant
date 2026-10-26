# 评分 v2 设计：七维固定锚点确定性打分

- 状态：设计稿，待评审（评审通过前不改产品代码）
- 日期：2026-10-27
- 依据：用户 2026-10-27 对第一轮 6 问（Q0–Q6）与第二轮 6 问（Q1–Q6）的确认
- 关联报告：.tmp/review/scoring-data.md、.tmp/review/scoring-methods.md、.tmp/review/scoring-change-surface.md
- 关联现状：backend/app/agent/tools.py:705-784（8 维打分）、tools.py:831-856（批内归一化）、backend/app/agent/weights.py:54-152
- 目标版本：SCORING_VERSION = scoring-v2；旧内核保留为 scoring-v1，flag-gated 切换

## 0. 一页摘要

把“推荐匹配分”从含糊的 8 维相对分，改为 **七维、固定锚点、可逐项复算、跨批稳定** 的绝对效用分：

1. 硬约束（在售、价格、品牌、车身、能源、座位、锁定/拒绝款型）完全沿用现有 SQL 下推，候选集不变。
2. 七维：用途适配、空间、用车成本、动力、安全配置、舒适配置、智能配置。
3. 预算从“打分维度”降为硬约束；不再出现“只给上限时预算分恒 1.0”。
4. 养护/可靠性/保值/口碑/碰撞成绩没有数据，一律不进分，不拿代理冒充。
5. 所有归一化使用版本化固定锚点，不使用“本批候选最大/最小”，同一款车在任何候选池中分数相同。
6. 缺数据统一按中性 0.5 计，同时返回数据完整度 coverage；完整性低于 0.5 标 low_evidence，不因缺数据扣分，也不让缺数据车靠少数高分项冲顶。
7. 排序后做确定性多样性约束：同车系最多 2 款，前 5 至少 3 个车系（候选不足时按固定顺序放宽）。
8. 新内核走 AGENT_SCORING_MODE=off|shadow|new 开关，默认 off；无数据库迁移，回滚=改环境变量。

## 1. 背景与现行问题

### 1.1 用户反馈（2026-10-27）

用户认为现行 8 维评分“并不合理”，要求通过工具与 skill 找一套合适方案，不确定处用 grilling 询问。已确认的问题层包括：各维口径不合理、缺数据仍扣分、同车系同质化、分数跨批不可比。

### 1.2 现行 8 维的结构性问题（证据）

| 问题 | 现状 | 证据 |
| --- | --- | --- |
| 批内归一化 | 舒适/智能按本批候选最大值归一，再重算总分；同一款车换候选池分数就变 | tools.py:831-856；docs/2026-10-24-variant-level-scoring.md:62-63 |
| 缺数据语义不一致 | 空间/动力缺失记 0.5，舒适/智能缺失记 0.0 参与加权 | tools.py:729-752、754-774 |
| 锚点硬编码 | 车长 4300–5200mm、功率 80–300kW 写死，无版本无出处 | tools.py:734、751 |
| 预算名不副实 | 硬过滤后预算分恒 1.0；只在只给上限/无预算时有变化 | tools.py:705-714 |
| 能耗不读能耗 | energy 只看 energy_type（1.0/0.7/0.4），不读油耗/电耗 | tools.py:737-744 |
| 安全无独立维度 | 安全/操控配置被并入 comfort 的标配项计数 | tools.py:315、754-774；weights.py:122-152 |
| 养护是代理 | maintenance = 品牌在库车系数 + 是否有销量记录，同品牌全系同分 | tools.py:776-781 |
| 同车系同质化 | 2026-10-24 生产逸动 3 款型同分 0.8816；修后仍只有 comfort/intel 区分 | docs/2026-10-24-variant-level-scoring.md |

### 1.3 数据事实（本地生产快照 backend/.tmp/eval-local.db，2026-10-03）

在售 5,132 款型的字段覆盖率：

| 字段 | 覆盖 | 用途 |
| --- | --- | --- |
| 官方指导价 | 100% | 硬约束 + 成本/价格带分桶 |
| 长/宽/高、轴距 | 100% / 99.8% | 空间 |
| 功率 | 99.9% | 动力 |
| 整备质量 | 90.7% | 动力 kW/t |
| 百公里油耗 | 60.1% | 用车成本 |
| 百公里电耗 | 38.6% | 用车成本 |
| 后备厢容积 | 约 55% | 空间（可选子项） |
| 座位数（seat_count） | 19.5% | 用途适配/座位硬约束（缺数据按 IS NULL 保留） |
| 整车质保 | 0 | 不进分 |
| 安全配置 ● 事实 | 87,560 条 / 6,554 款型 | 安全配置 |
| 内部+外部+操控配置 ● | 约 21 万条 | 舒适配置 |
| 智能/辅助驾驶 ● | 42,948 条 / 6,278 款型 | 智能配置 |

结论：空间、动力、安全配置、舒适配置、智能配置、用车成本都能做到款型级、近全量或可披露覆盖；质保/保值/口碑/碰撞成绩没有，不进入评分。

## 2. 决策记录（用户确认）

| 编号 | 决策 | 结论 |
| --- | --- | --- |
| Q1 | 维度集合 | 七维（A 方案）：用途适配、空间、用车成本、动力、安全配置、舒适配置、智能配置。预算/能源偏好/品牌/车身只作硬约束；养护/可靠性/保值/口碑/碰撞成绩不进分 |
| Q2 | 默认权重 | 安全 22%、用车成本 18%、空间 15%、动力 13%、舒适 12%、智能 10%、用途 10%；用户强调某维时 +20 个百分点再归一 |
| Q3 | 同价位比较 | 七维全部按同价位比较（2026-10-27 01:19 用户确认）：每个价格带内分别计算七维锚点；空间/动力/成本不再用全库绝对分位 |
| Q4 | 缺数据 | 缺失维按中性 0.5 计，返回 coverage；coverage<0.5 标 low_evidence；不扣分、不因缺数据进不了前 5，也不让缺数据车虚高 |
| Q5 | 用车成本 | 92# 汽油 7.5 元/L、家充 0.6 元/kWh；PHEV/增程按 50% 用电 + 50% 亏电油耗；EV 用电耗，ICE/HEV 用油耗；文案标“按官方参数估算” |
| Q6 | API 与红线 | 接受 weights_used 键集变更，新增 scoring_version；安全成为显式维度，正式豁免“安全不可排序”旧红线，并在设计文档/ADR/测试中显式记录 |

Q3 已于 2026-10-27 01:19 由用户确认扩展为“全部七维同价位”，本文与实现均按此执行；原“安全/舒适/智能同价位、其余全库”的方案作废。

## 3. 方案总览

链路：

硬约束 SQL 下推 → 全量物化候选（不变）→ 逐款型取事实 → 七维效用（固定锚点）→ 缺数据中性 + coverage → 加权平均 → 确定性排序 → 多样性后处理 → matched/tradeoffs 叙事 → API/前端

与 v1 的区别：

| v1 | v2 |
| --- | --- |
| 8 维含 budget/energy/maintenance | 7 维，预算/能源作硬约束，安全独立 |
| comfort/intel 批内归一化 | 全部固定锚点，无批统计 |
| 缺数据 0.5 或 0.0 不一致 | 统一 0.5 + coverage + low_evidence |
| 无多样性后处理 | 同车系≤2，前 5≥3 车系 |
| 无 score 版本 | scoring_version=scoring-v2，锚点版本化 |
| 安全不可映射 | 安全显式维度（仅配置，不宣称碰撞安全） |

不做什么（延续 2026-10-20 计划 §六，除本次显式豁免项）：不引 agentic RAG/HyDE；不接第二数据源；不改硬约束下推；不重构 series_constraints.py/rag/service.py/answer_contract.py；不承诺款型级实时；不新建第三份规划文档；不做 LLM 排序、不做学习型权重（等人工成对标签）。

## 4. 七维定义

统一记号：

- tri(x; p10, p50, p90)：把原始值映射到 0–1 的分段线性函数。x≤p10 取 0；p10<x≤p50 取 0.5*(x-p10)/(p50-p10)；p50<x≤p90 取 0.5+0.5*(x-p50)/(p90-p50)；x>p90 取 1。锚点必须 p10<p50<p90，否则退化处理。
- 低优指标（用车成本）：u = 1 - tri(x)。
- 所有锚点来自版本化 JSON，不来自请求候选集。

### 4.1 用途适配（usage）

- 激活条件：profile.usage 非空且能识别到至少一个用途词；否则该维不激活，权重不计，其余维度归一化。
- 识别词：通勤、上下班、家庭、家用、带娃、长途、自驾、商务、接待（同义扩展沿用 USAGE_HINTS 并扩展）。
- 每个用途给 2–3 个可解释子项，子项等权平均；未知字段按 0.5 计。
- 规则表：

| 用途 | 子项 1 | 子项 2 | 子项 3 |
| --- | --- | --- | --- |
| 通勤 | 能源：BEV/PHEV/HEV=1，ICE=0 | 车身：sedan=1，hatchback=0.8，SUV=0.5，MPV=0 | 车长：≤4700mm=1，4700–4900=0.5，>4900=0 |
| 家庭 | 车身：SUV/MPV=1，sedan=0.5，其他=0 | 座位：≥5=1，未知=0.5，<5=0 | 后备厢：≥500L=1，400–500=0.7，300–400=0.4，<300=0，未知=0.5 |
| 长途 | 能源：ICE/HEV/PHEV/EREV=1，BEV=0.4 | 续航：≥600km=1，500–600=0.8，400–500=0.5，<400=0.2，未知=0.5 | 后备厢：同家庭 |
| 商务 | 车身：sedan/MPV=1，SUV=0.3 | 车长：≥4900mm=1，4700–4900=0.6，<4700=0.2 | 智能配置项数：≥中位=1，否则 0.5（仅作弱信号） |

- 多个用途词时取各用途得分的平均。
- 用途是规则匹配，不是“事实覆盖率”；measure=true（能按规则算），证据里写“按用途规则”。
- 不把“用途”当作硬过滤，避免用户说“家庭”就排除轿车。

### 4.2 空间（space）

原始子项：

- 轴距 wheelbase_mm（覆盖 99.8%）
- 车长 length_mm（100%）
- 车宽 width_mm（从“长*宽*高(mm)”解析，100%）
- 后备厢容积 trunk_L（约 55%，可选）

公式：

space_u = (0.45*tri(wheelbase) + 0.30*tri(length) + 0.15*tri(width) + 0.10*tri(trunk)) / 权重和

- trunk 缺失时，权重和取 0.90，并只用前三项；trunk 存在时权重和 1.00。
- measure：轴距或车长至少一个存在；两者都缺则整维缺失（中性 0.5）。
- 锚点：按价格带分桶的在售款型 p10/p50/p90（同价位比较，越大越好）。
- 不按车身类型分桶：用户说“空间”时，同价位内 SUV/MPV 天然占优是合理的。
- 同车系尺寸相同是事实，不强行制造款型差异；款型差异由配置/成本/动力承担。

### 4.3 用车成本（running_cost）

原始成本 c = 元/100km：

- ICE/HEV：c = 油耗(L/100km) × 7.5
- BEV：c = 电耗(kWh/100km) × 0.6
- PHEV/EREV：
  - 同时有油耗与电耗：c = 0.5 × 油耗 × 7.5 + 0.5 × 电耗 × 0.6
  - 只有一种：用那一种，并在证据里标“仅按油耗/仅按电耗估算”
- 油耗键优先级：WLTC综合油耗 > NEDC综合油耗 > CLTC综合油耗 > 最低荷电状态油耗（PHEV/EREV 优先最低荷电状态油耗，其余车优先综合油耗）
- 电耗键：百公里耗电量(kWh/100km)；若同一款型有多个工况值，按 CLTC > WLTC > NEDC 优先，并在证据中记录 cycle
- 只接受白名单 cycle：CLTC/NEDC/WLTC；其他/空 cycle 仍可用但标“工况未注明”，不混算不同键
- 同一 fact_key 出现多个不同值（冲突键）时，跳过该键；全部冲突则整维缺失
- measure：至少一种有效能耗事实
- 锚点：按价格带分桶的在售款型成本 p10/p50/p90（元/100km，同价位比较，越低越好）；u = 1 - tri(c)
- 成本不按能源分桶：元/100km 本身可比；用户有能源偏好时由硬约束过滤
- 文案必须标“按官方参数估算，油价/电价假设见附录”

### 4.4 动力（power）

原始值：功率质量比 kW/t = 功率(kW) / 整备质量(kg) × 1000

功率选取优先级（同一款型内）：

1. 系统综合功率(kW)
2. 电动机总功率(kW)
3. 最大功率(kW)
4. 最大马力(Ps) 换算：kW = Ps × 0.73549875

- 同一键多值且不一致 → 跳过该键；全部冲突 → 整维缺失。
- 整备质量缺失 → 整维缺失（不拿 0-100 混算，避免量纲不一致）。
- measure：功率与整备质量都有
- 锚点：按价格带分桶的在售款型 kW/t p10/p50/p90（同价位比较，越大越好）。
- 不按能源分桶：kW/t 是统一物理量；同价位内 BEV 偏高是事实，用户能源偏好由硬约束处理。

### 4.5 安全配置（safety）

定位：**安全配置水平（配置代理）**，不是碰撞安全，不宣称“最安全”。

关键项 10 组，每组在“安全配置”类目下匹配任一 fact_key 且值含 ● 即计 1 项：

1. 主/副驾安全气囊
2. 前排侧气囊
3. 前后排头部气帘
4. 胎压监测
5. 车身稳定控制（ESP/ESC）
6. 主动刹车/主动安全系统
7. 前方碰撞预警
8. 车道偏离预警/车道保持辅助
9. 并线辅助/盲区监测
10. ISOFIX 儿童座椅接口

原始分 raw = 命中组数（0–10）。

- measure：该款型“安全配置”类目下至少有 5 条事实；否则整维缺失。
- 锚点：按价格带分桶的 raw p10/p50/p90（同价位比较）。
- 解释文案只能写“同价位安全配置水平”，不得写碰撞星级/安全性结论。
- 未来接入 C-NCAP/E-NCAP 后另立维度，不在本版承诺。

### 4.6 舒适配置（comfort）

原始分 raw = “内部配置 + 外部配置 + 操控配置”三个类目中值含 ● 的事实条数。

- 明确排除“安全配置”类目，避免与 safety 双计。
- 生产没有“舒适性”类目（数据审计已确认），不要按该名称取数。
- measure：上述三个类目至少有一条事实。
- 锚点：按价格带分桶的 raw p10/p50/p90（同价位比较）。
- 项数是无加权计数，作为“配置丰富度”代理；不宣称座椅舒适性/隔音/底盘质感。

### 4.7 智能配置（intelligence）

原始分 raw = “智能/辅助驾驶”类目中值含 ● 的事实条数。

- 生产类目名以“智能/辅助驾驶”为准；“智能驾驶”“智能座舱”是否存在未经生产验证，不依赖。
- measure：该类目至少有一条事实。
- 锚点：按价格带分桶的 raw p10/p50/p90（同价位比较）。
- 不宣称 L2 等级、芯片算力、OTA 能力；只写“智能配置水平”。

## 5. 聚合、缺数据、排序与多样性

### 5.1 权重

基础权重（归一化前）：

| 维度 | key | 标签 | 默认权重 |
| --- | --- | --- | --- |
| 安全配置 | safety | 安全配置水平 | 0.22 |
| 用车成本 | running_cost | 用车成本 | 0.18 |
| 空间 | space | 空间 | 0.15 |
| 动力 | power | 动力 | 0.13 |
| 舒适配置 | comfort | 舒适配置 | 0.12 |
| 智能配置 | intelligence | 智能配置 | 0.10 |
| 用途适配 | usage | 用途适配 | 0.10 |

- usage 未激活时，从分母中移除，其余 6 维按比例归一（安全 0.244、成本 0.200、空间 0.167、动力 0.144、舒适 0.133、智能 0.111）。
- profile.weights 是绝对覆盖（沿用现有语义），只接受 v2 键；旧键 budget/energy/maintenance 忽略并记录 warning，避免静默误用。
- 用户强调某维：weight = base + 0.20，CEILING=1.0，再归一化。保持“强调必须能翻转排序”的现有性质。
- weights_used 返回归一化后的有效权重（仅激活维度）；这是对外契约变更，见 §8。

### 5.2 缺数据与分数

设激活维度集合 D，权重 w_d，效用 u_d，measured_d：

- 若 measured_d：û_d = u_d
- 若未 measured：û_d = 0.5（中性）
- U = Σ_d w_d û_d / Σ_d w_d
- coverage φ = Σ_{d: measured_d} w_d / Σ_d w_d
- score_low = Σ_d w_d * (u_d if measured else 0) / Σw
- score_high = Σ_d w_d * (u_d if measured else 1) / Σw
- low_evidence = φ < 0.5
- 若 D 为空（理论上不会）：U=0.5，φ=0

性质：

- 缺数据不扣分，也不会让缺数据车靠少数高分项冲顶（被拉回 0.5）。
- 同一画像、同一款型、同一锚点版本 → 分数与候选池无关。
- 全缺失 → U=0.5，φ=0，low_evidence=true。
- score 仍保留 4 位小数，范围 (0,1)；score=0 继续保留为“非评分场景”哨兵（engine.py:2398-2408）。

### 5.3 排序

确定性排序键（从高到低）：

1. U（round 6 位）
2. coverage φ
3. 最新月销量（retail 优先，portal 回退）
4. variant_id（升序，保证稳定）

- 无软信息画像（usage/passengers/body_type/energy_preference 都为空）继续按最新月销量排序，保留 README“零软线索按近月销量”的承诺；U 仍计算用于展示/调试，但不改变排序。
- 不用随机数、不用当前时间；同输入逐位相同。

### 5.4 多样性

- 按排序遍历候选，接受条件：该车系已出现次数 < 2。
- 收集到 limit（默认 5）个即停止。
- 若因上限无法收满：先放宽到每车系 < 3，再放宽到不限；每次放宽都是确定性的。
- 当候选中有 ≥3 个车系时，前 5 至少 3 个车系（数学上由“每车系≤2”保证）。
- 记录 diversity_relaxed 级别，便于评测。
- 不做品牌上限（用户已确认只限车系），品牌偏好由硬约束处理。

## 6. 锚点与生成脚本

### 6.1 锚点内容

1. 价格带锚点（七维全部同价位）：
   - 价格带（万元）：[0,8)、[8,10)、[10,12)、[12,15)、[15,18)、[18,22)、[22,28)、[28,35)、[35,50)、[50,+∞)
   - 每带分别给 space（wheelbase/length/width/trunk）、power（kW/t）、running_cost（元/100km）、safety/comfort/intelligence 计数、usage 所需子项 的 p10/p50/p90 与样本数 n
   - 若某带总样本 n<30：该带整体回退全库锚点，并在锚点文件标 fallback；单指标样本不足 30 记入 fallback_subkeys
2. 全局锚点（全库在售款型）：只作为价格带样本不足时的兜底，不参与正常打分。

### 6.2 生成脚本

- 新增 backend/tools/gen_scoring_anchors.py：
  - 输入：数据库连接（SQLite/PG 均可）、快照日期/commit、价格带配置
  - 只取在售款型 + 当前官方指导价 + 所需 spec_facts
  - 复用 tools.py 的取数口径（键名、● 判定、冲突键规则），避免两套解析
  - 输出 backend/app/agent/scoring_anchors_v2.json：anchor_version、generated_at、source、price_bands、global、by_price_band、fallback 标记
  - 打印每带样本数、p10/p50/p90、被 fallback 的带
- 锚点文件提交 git；每次目录刷新（月度/大批量导入）重跑，生成新 anchor_version。
- 漂移告警：新锚点 p50 相对旧版变化 >10% 时，脚本非 0 退出并要求人工确认；CI 加锚点结构校验（p10<p50<p90、价格带连续、版本字段存在）。
- 初始锚点用 backend/.tmp/eval-local.db（2026-10-03 生产快照，5,132 在售）生成，标 catalog-2026-10-03；上线前用当前生产库重跑一次。

## 7. 实现结构

### 7.1 新模块

backend/app/agent/scoring_v2.py（纯函数，不 import tools/engine）：

- DIM_KEYS/DIM_LABELS、DEFAULT_WEIGHTS_V2、WEIGHT_RAISE_V2、PRICE_BANDS
- load_anchors()、tri()、anchor_utility()
- extract_features(variant, facts, price, anchor_set) -> dict
- dimension_utilities(features, profile, anchors) -> (utilities, measured flags, evidence)
- aggregate(utilities, weights) -> (U, coverage, score_low, score_high, parts)
- rank_and_diversify(rows, limit) -> rows
- 只依赖标准库 + backend/app/agent/weights.py（v2 常量放在 weights.py，保持单一真值源）；不产生循环依赖。

### 7.2 tools.py 改造

- 硬约束 SQL（tools.py:468-537）一行不改；候选集等价测试保持绿。
- 扩展事实加载：现有只加载座位/长度/功率键 + 舒适/智能类目（tools.py:586-599）。v2 需要：
  - 参数类：轴距、长*宽*高、整备质量、油耗、电耗、续航、电池、系统综合功率/电动机总功率/最大功率/最大马力、后备厢容积
  - 配置类：安全配置、内部配置、外部配置、操控配置、智能/辅助驾驶
  - 仍按候选 variant_id 批量 IN 查询，避免 N+1；必要时按 category 分批。
- 新增 AGENT_SCORING_MODE 读取（收敛在 recommendation_tool 入口）：
  - off：v1 原逻辑
  - shadow：v1 返回给用户；同时算 v2，结构化日志记录 scoring_version、candidate_hash、v1/v2 top5、score 差、coverage、耗时；采样率可配
  - new：v2 返回
- 候选 dict 增加 coverage、low_evidence；include_dims=True 时返回 score_parts（不再泄露到生产响应）。
- weights_used 返回 v2 归一化权重（仅激活维度）。
- 保留 score=0 哨兵、limit 只截断、无 SQL LIMIT。

### 7.3 engine.py / 叙事

- top_dim、_DIM_LABELS 引用 v2 标签；确定性回退文案（engine.py:2864-2881）改为新维度。
- _explain 的候选上下文增加能耗/续航/轴距等 measured facts（数据审计建议），避免“评分改了、解释仍说库里没有”。
- matched 生成规则改为新维度：
  - 安全：同价位安全配置水平（例如“关键安全配置 8/10 项”）
  - 成本：按官方参数估算的元/100km
  - 空间/动力/舒适/智能：相对锚点的分位描述，数字必须来自工具结果
- tradeoffs 只对用户明确提过的维度、差距≥0.2（沿用）；维度标签更新。
- 文案不出现未定义数字；沿用 answer_contract 的数字白名单校验。

### 7.4 soft_prefs / turn / profile

- soft_prefs.py:227-277 的封闭枚举改为 v2 七维；SOFT_PREF_VERSION 递增。
- 安全类关键词（安全/气囊/碰撞/主动安全）改为映射 safety；这解除了旧红线的实现点，但只允许映射到“安全配置水平”，提示词中禁止承诺碰撞安全。
- turn.py:315-324 强调权重改用 v2 基础权重 + RAISE；profile.py 的累加/CEILING/top_dim 逻辑不变，只换键集与默认值。
- weights.py:122-152 的“安全不映射”注释删除，替换为 v2 设计链接与“安全仅配置代理”的限制说明。

## 8. API / 前端 / 契约

- POST /api/v1/recommendations 与 Agent 主链共用 recommendation_tool，同一个开关，避免分叉。
- RecommendationOut 增加 scoring_version: str（默认空/scoring-v1 兼容旧响应）；variants 增加 coverage: float、low_evidence: bool（只增不改）。
- weights_used 键集从 8 键变为 7 键（usage 未激活时为 6 键），属破坏性变更：
  - 仓内 web 未调用 /recommendations（web/lib/api.ts 只有类型；业务只用 AgentMessageOut），无内部破坏。
  - 外部消费者未知；用 scoring_version 标识，文档/README 注明 v0.1.0 内变更。
  - 不保留旧键名映射（budget/energy/maintenance 在新模型中没有对应语义，映射会制造错误解释）。
- AgentMessageOut 可增加 coverage 可选字段；前端暂不强制使用。
- 前端展示（Phase 2）：score 保持 0–1 两位小数；卡片增加“数据完整度 X%”，top-3 贡献维度；不把 score 改百分制，避免“考试分”误解。
- score=0 哨兵、matched/tradeoffs/seat_verified 字段保留。

## 9. 测试计划

### 9.1 必须保持的守卫测试（不改）

- backend/tests/test_candidate_scan.py：无 SQL LIMIT、limit 只截断、候选集不变。
- backend/tests/test_seat_count_pushdown.py：座位下推等价、IS NULL 保留、85% 未回填。
- backend/tests/test_vehicles_sql_pushdown.py：/vehicles 下推等价。
- backend/tests/test_tradeoff_user_scope.py、test_tradeoffs_antifabrication.py：只报用户提过的维度、不编造。
- answer_contract 相关测试：叙事数字可溯源。

### 9.2 需要重写期望的测试（钉旧实现）

- test_recommendation_scoring.py：14 个曲线用例按新公式重算；保留“单维差异→可解释分差”的方法论（用 weights_used 推导）。
- test_usage_weight_effect.py：usage 权重改为 0.10/强调 0.30，重算翻转条件。
- test_weights_single_source.py：默认权重/键集/标签更新；保留单源、CEILING/RAISE、词表子集性质。
- test_recommendation.py：weights_used 键集与覆盖语义。
- test_top_dim.py：默认最高维从 budget 变 safety；top_dim 仍不改排序。
- test_agent.py:339-440：强调抬高、覆盖语义、CEILING。
- test_recommendation_variant_scoring.py：comfort 不再包含安全配置；安全独立；款型可区分仍保留。
- test_brand_scale_full_catalog.py：maintenance 移除后，改为“销量仅用于无软信息排序/同分兜底”的全库口径测试。
- test_soft_prefs.py / test_route_emphasis_beats_advice.py / test_agent.py 安全用例：安全关键词现在允许映射，但断言“只映射到安全配置，不承诺碰撞安全”。
- test_tradeoffs_rendering_rate.py / test_tradeoffs_antifabrication.py：GAP 与标签随新分数量纲调整。
- backend/tools/eval_tradeoffs.py、eval_softpref_ab.py：镜像 v2 内核，否则评测自判失效。

### 9.3 新增测试（scoring v2 不变量）

- 批不变性：候选 c 在池 P 与 P∪Q 中七维 utility 与 U 完全相同（现行实现必失败，作为回归）。
- 候选集等价：矩阵画像下 v1/v2 的候选 id 集合与 candidates_scanned 逐位相同。
- 分数可复算：include_dims=True 取 parts/weights/coverage，按公开公式重算 U 与返回 score 相等（容差 1e-9）；生产响应不含 score_parts。
- 缺数据中性：每维构造缺事实款型，断言 û=0.5、measured=false、coverage 下降、不因缺数据掉出候选。
- 全缺失：U=0.5、coverage=0、low_evidence=true。
- 确定性：固定 8–12 个画像 + 固定种子库，top5（id+score+coverage）哈希等于快照；重复运行逐字节一致。
- 多样性：前 5 同车系≤2；候选足够时 distinct_series≥3；放宽顺序确定。
- 锚点：版本字段、价格带连续、p10<p50<p90、无请求内统计量。
- 安全口径：安全分只来自安全配置类目；comfort 不再包含安全配置；文案不出现碰撞/星级。
- 开关一致性：AGENT_SCORING_MODE=new 时 /recommendations 与 Agent 主链同分同序。
- shadow：日志字段存在且可解析。

## 10. 离线评测与验收门槛

### 10.1 数据与画像

- 快照：backend/.tmp/eval-local.db（5,132 在售款型）或当前生产只读快照；冻结 DB hash。
- 画像集：60 个场景，覆盖：
  - 预算档：8/10/12/15/18/22/28/35 万
  - 能源：ICE/BEV/PHEV/EREV/不限
  - 车身：sedan/SUV/MPV/不限
  - 用途：通勤/家庭/长途/商务/未说
  - 特殊：只给上限、只给下限、无预算、强调安全/空间/成本、拒绝款型、锁定车系
- 每个场景记录：必须出现（must_include）、必须不出现（must_exclude，硬约束违规）、可接受集合；不预写完整排名，避免把旧实现当金标。

### 10.2 指标

- 硬约束违规 = 0。
- 候选集与 v1 逐位相同（I1）。
- 批不变性 100%。
- 分数可复算 100%。
- must_include recall@5：v2 ≥ v1（non-inferior），且绝对值 ≥0.85。
- 多样性：top5 同车系≤2；有 ≥3 车系的场景中 distinct_series≥3 的比例 ≥90%。
- 覆盖：平均 coverage ≥0.75；low_evidence 候选占比 ≤15%。
- 维度区分度：全库每维 utility 的 p10–p90 跨度 ≥0.20（否则说明锚点饱和）。
- 人工金标（若人力允许）：30 画像 × 每位抽 10 对人工盲评（300 对），pairwise accuracy ≥0.60，且 v2 ≥ v1；Cohen kappa 报告。
- 延迟：离线 p95 不高于 v1 的 1.2 倍，且整体 recommend 链路 p95 <4.5s（当前 p50 3.09s、max 4.13s，见 refactoring-roadmap.md:424-439）。
- 评测纪律：冻结候选池（全量 5000，不用 top5 比较）、冻结锚点/快照/画像、同池 paired 比较、按覆盖率/价格/能源分层报告；禁止跨批均值比较。

### 10.3 工具

- 新增 backend/tools/eval_scoring_v2.py：一次跑 v1/v2，输出上述指标 JSON + markdown 报告；不含 LLM。
- 复用 eval_softpref_ab.py 的候选集不变硬红线、eval_tradeoffs.py 的每句可验证判据、eval_agent.py 的硬约束检查。
- 评测结果写入 docs/ 或 .tmp/review，不提交题库原始数据（题库不入 git 的既有规则）。

## 11. 迁移、灰度与回滚

- 无数据库迁移：不新增列/表；锚点是代码内 JSON；spec_facts 已存在。若后续性能需要物化特征表，另立设计。
- Phase 0 设计评审 + ADR-0008（本文通过后）。
- Phase 1 实现 v2 + 开关 off；CI 全绿；v1 逻辑保留。
- Phase 2 生成锚点（生产快照）→ 离线评测 → 人工评审 20 个典型场景。
- Phase 3 影子：AGENT_SCORING_MODE=shadow；线上仍 v1；记录差异与耗时；检查无异常。
- Phase 4 切换：staging new → 生产 new；保留 v1 至少一个发布周期；观察错误率/耗时/用户反馈。
- 回滚：AGENT_SCORING_MODE=off，无需数据回滚；scoring_version 留在响应/日志中便于切分。
- 监控：日志字段 scoring_version、candidate_hash、top5_diff、score_delta、coverage_mean、low_evidence_rate、latency；锚点版本变更单独告警。
- 灰度若需按用户比例：本仓无 per-user flag 基建；先用 env 全量切换 + 影子兜底；要比例灰度另立小改动，不在本版范围。

## 12. 文档红线豁免与 ADR

本次显式豁免以下既有约束，必须留下书面记录，不能静默改：

1. docs/2026-10-20-architecture-and-product-plan.md:229-237 §六 第 3 条“不改 recommendation_tool 硬约束下推与 8 维打分”：
   - 硬约束下推仍不改；8 维打分被本设计取代，需在该条追加“2026-10-27 用户决策，豁免 8 维部分，见 docs/2026-10-27-scoring-v2-design.md 与 ADR-0008”。
2. docs/2026-10-21-deferred-items-execution-plan.md:91 的同类红线描述同步追加豁免。
3. docs/adr/0003-data-scope-parity-with-autohome.md:15 的“8 维确定性打分”改为“评分 v2 的确定性打分（七维，见 ADR-0008）”。
4. 新增 docs/adr/0008-scoring-v2-seven-dimensions.md：
   - 决策：七维、固定锚点、缺数据中性、安全配置独立维度、预算/能源作硬约束。
   - 背景：批内归一化、缺数据不一致、预算恒 1.0、能耗不读能耗、安全/养护无维度。
   - 后果：weights_used 契约变更；安全仅配置代理、不承诺碰撞；旧 8 维测试与文案需更新。
   - 替代方案：保留旧 8 键（否，语义错误）、学习型权重（推迟，缺标签）、LLM rerank（不做，违反 ADR-0002）。
5. docs/2026-10-24-variant-level-scoring.md 顶部加“已被 scoring-v2 取代”的链接，保留历史记录。
6. README 中“8 维打分”相关描述改为七维并链接设计文档；docs/optimization-progress.md 新增一节记录本次变更（不再改历史章节）。
7. weights.py:122-152 注释、test_soft_prefs.py:97、test_route_emphasis_beats_advice.py:127-129、test_agent.py 的 safety 用例同步修改，并在测试名/注释中写明“安全配置水平，非碰撞安全”。

## 13. 风险与未验证项

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 锚点随目录漂移 | 分数整体抬高/压低 | anchor_version + 重跑脚本 + p50 漂移 >10% 告警 |
| 价格带样本稀疏（高分位车） | 同价位分位不稳 | n<30 合并相邻带/全库 fallback，锚点文件记录 |
| 配置计数被低频项稀释 | 舒适/智能分虚高 | 先上线计数版；观察分布，后续可换关键项加权（需新设计） |
| 能耗工况混算 | 成本比较有噪声 | cycle 白名单 + 优先级 + 证据标注；缺数据中性 |
| 安全配置≠碰撞安全 | 用户误解“最安全” | 标签/文案统一“安全配置水平”，禁止碰撞星级承诺 |
| 缺数据中性掩盖不确定 | 低覆盖车排中间 | coverage/low_evidence/score_range 披露；<0.5 不进 best_fit 角色 |
| 默认权重主观 | 排序争议 | 文档化理由 + 用户强调可覆盖 + 后续人工标签校准 |
| weights_used 契约破坏 | 外部消费者报错 | scoring_version + 文档；当前无已知消费者 |
| shadow CPU 翻倍 | 延迟 | 采样/限流；shadow 仅观察 |
| 未验证：生产当前键名/覆盖 | 锚点与取数偏差 | 上线前用当前生产快照重跑 gen_scoring_anchors.py，先 dry-run 打印 |
| 未验证：外部 /recommendations 消费者 | 契约风险 | 发布说明 + 版本字段；必要时兼容层另立 |

## 14. 分期任务与估算

| 阶段 | 交付 | 估时（人日） |
| --- | --- | --- |
| 0 | 本设计评审 + ADR-0008 | 0.5 |
| 1 | 锚点生成脚本 + 初始锚点 JSON + 校验 | 1 |
| 2 | scoring_v2.py 纯函数 + 单测 | 2 |
| 3 | tools/engine/soft_prefs/turn 接线 + flag + shadow 日志 | 2 |
| 4 | 测试重写与新增不变量测试 | 3 |
| 5 | 离线评测脚本 + 60 场景 + 报告 | 2 |
| 6 | 人工金标/盲评（若做） | 2 |
| 7 | 前端 coverage/分维展示（可后置） | 1 |
| 8 | README/计划/ADR/台账文档 | 0.5 |
| 合计 | | 约 12–14 人日（不含人工标注等待） |

建议顺序：0 → 1 → 2 → 4（先写不变量测试）→ 3 → 5 → 6 → 7 → 8；每个阶段独立可回滚。

## 15. 验收清单

- [x] 设计评审通过，Q3 已确认为“全部七维同价位”（2026-10-27 01:19）
- [ ] ADR-0008 合入，红线豁免写入计划文档
- [ ] 锚点脚本可在生产快照上 dry-run，输出每带 n 与 p10/p50/p90
- [ ] v2 单测覆盖 §9.3 全部不变量
- [ ] v1 守卫测试零改动作仍绿
- [ ] 离线评测：硬违规 0、候选集等价、批不变、可复算、多样性达标、coverage 达标
- [ ] 性能：p95 增幅 ≤20%，绝对 <4.5s
- [ ] shadow 日志可解析，无异常
- [ ] 文档/README/测试名同步，无“8 维”“安全不可排序”残留
- [ ] 回滚演练：AGENT_SCORING_MODE=off 立即恢复 v1

## 16. 评审待确认

1. ~~Q3 范围~~ 已定稿（2026-10-27 01:19）：七维全部同价位；锚点生成器与实现均按此执行。
2. 默认权重表是否照 22/18/15/13/12/10/10 执行。
3. 用车成本假设（7.5 元/L、0.6 元/kWh、PHEV 50% 用电）是否作为产品默认，是否需要在 UI 明示。
4. 是否接受 /recommendations 的 weights_used 键集变更（v0.1.0 内）。
5. 人工金标能否安排（30 画像 × 10 对）；若不能，Phase 2 只做结构/非劣化评测，v2 先 shadow，不切默认。

## 附录 A：v1 → v2 维度映射

| v1 key | v1 含义 | v2 处理 |
| --- | --- | --- |
| budget | 预算区间中点接近度 | 删除；预算只作硬约束 |
| usage | 3 条规则命中率 | 保留并扩展为七维之一，按用途规则表 |
| space | 车长线性 4300–5200 | 改为轴距/长/宽/后备厢固定锚点 |
| energy | 能源类型 1.0/0.7/0.4 | 改为 running_cost（油耗/电耗→元/100km） |
| power | 功率线性 80–300kW | 改为 kW/t 固定锚点 |
| comfort | 5 类目（含安全）● 计数，批内归一 | 改为 3 类目（不含安全）● 计数，价格带锚点 |
| intelligence | 3 类目 ● 计数，批内归一 | 改为“智能/辅助驾驶”● 计数，价格带锚点 |
| maintenance | 品牌规模+销量代理 | 删除；销量仅用于无软信息排序/同分兜底 |
| （新增） | — | safety：10 组关键安全配置计数，价格带锚点 |

## 附录 B：安全关键项匹配规则

在“安全配置”类目内，对每组用关键词包含匹配 fact_key；值含 ● 即命中；每组最多计 1：

1. 主/副驾气囊：驾驶座安全气囊、副驾驶安全气囊、主/副驾安全气囊
2. 前排侧气囊：前排侧气囊
3. 头部气帘：前/后排头部气囊(气帘)、头部气帘
4. 胎压监测：胎压监测、胎压显示
5. 车身稳定：车身稳定控制、ESP、ESC、牵引力控制
6. 主动刹车：主动刹车、主动安全系统、自动紧急制动
7. 前方碰撞预警：前方碰撞预警、碰撞预警
8. 车道辅助：车道偏离预警、车道保持辅助、车道居中保持
9. 并线辅助：并线辅助、盲区监测、盲点监测
10. ISOFIX：ISOFIX、儿童座椅接口

未命中的组按 0 计；类目事实 <5 条整维缺失（中性），避免把“数据缺失”当成“没有配置”。

## 附录 C：数据键清单（v2 取数）

- 参数类（category 参数信息）：
  - 轴距(mm)、长*宽*高(mm)
  - 整备质量(kg)
  - 系统综合功率(kW)、电动机总功率(kW)、最大功率(kW)、最大马力(Ps)
  - WLTC综合油耗(L/100km)、NEDC综合油耗(L/100km)、CLTC综合油耗(L/100km)、最低荷电状态油耗(L/100km)
  - 百公里耗电量(kWh/100km)
  - CLTC纯电续航里程(km)、CLTC综合续航(km)、WLTC纯电续航里程(km)
  - 后备厢容积(L)
  - 座位数(个)（已有 seat_count 物化列优先）
- 配置类：安全配置、内部配置、外部配置、操控配置、智能/辅助驾驶。
- 所有取值必须带 source_id/cycle/unit 证据；冲突键按 §4.3/§4.4 规则排除，不静默取第一个。

## 附录 D：与 2026-10-24 款型级修复的关系

- 2026-10-24 的修复（comfort/intel 从布尔改标配项数、补安全/操控类目、生产 0.8787/0.8508/0.8389）解决了“同车系同分”的一半，但保留了批内归一化与 safety 混入 comfort。
- v2 保留“标配项数”思想，改为绝对锚点，并把安全拆出；因此 v2 是那次修复的正式收口，不是回退。
- 旧文档保留为历史，顶部加 superseded 链接。
