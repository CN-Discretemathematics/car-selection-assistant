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
 * 价格缺失时的文案。**2026-10-03 产品拍板：与 `MISSING_VALUE_LABEL` 统一。**
 *
 * 此前是 `"暂无"`，而缺失的能源/参数/在售款型用 `MISSING_VALUE_LABEL`
 * （"官方资料未披露"）——同一个「库里没有」的事实有两种诚实措辞。
 * 「暂无」的问题在于它**指向不明**：既像"加载中"，也像"没有这款车"，
 * 而真实原因是**来源方没给**。「官方资料未披露」明确指向来源方，
 * 与本项目「缺失要显式、不得沉默跳过」的原则一致。
 *
 * `AgentChat.tsx` 此前已在调用点自己兜了长文案，说明作者也是这个判断。
 * 现在统一到常量，`formatPrice` / `formatPriceRange` 走同一口径。
 */
export const PRICE_MISSING_LABEL = MISSING_VALUE_LABEL;
