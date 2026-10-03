/**
 * 用户可见的**诚实性文案**唯一来源（2026-10-02 抽取）。
 *
 * 「官方资料未披露」是本项目的核心诚实性标记——设计原则第 1 条是
 * 「事实只来自数据库与工具返回值」，其反面就是：查不到就明说，绝不填 0、
 * 绝不由模型补全。后端同名常量在
 * `backend/app/common/enums.py` 的 `MISSING_VALUE_LABEL`，
 * 两侧必须一致——**改这里就要同步改那里**。
 *
 * 此前它是散落在页面里的字面量（`compare/page.tsx` 与
 * `vehicles/[series_id]/page.tsx` 各一份 `MISSING_LABEL` 常量），
 * 改文案要改多处，而漏改一处就会出现「同一种缺失、两种说法」。
 */
export const MISSING_VALUE_LABEL = "官方资料未披露";

/**
 * ⚠️ 已知的不一致，**本轮刻意不改**（属产品口径决策，不是重构）：
 *
 * `formatPrice(null)` 返回 `"暂无"`，而缺失的能源/参数/在售款型返回
 * `MISSING_VALUE_LABEL`。同一个「库里没有」的事实有两种诚实措辞。
 * `AgentChat.tsx` 已经在调用点自己兜了 `MISSING_VALUE_LABEL`，
 * 说明作者认为价格缺失也该用长文案——但改 `formatPrice` 会影响
 * 所有调用点的用户可见文案。
 *
 * 已记入 `docs/refactoring-roadmap.md` 的 P2「不修清单」，
 * 需产品侧拍板后再统一。
 */
export const PRICE_MISSING_LABEL = "暂无";
