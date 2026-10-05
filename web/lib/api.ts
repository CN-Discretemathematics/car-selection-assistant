/** 后端 API 类型与访问辅助（的响应结构）。 */

// 导入路径**必须带 .ts 扩展名**：lib/ 下的模块要被 Node 测试 runner
// （`node --experimental-strip-types --test`）直接 import，而 ESM 解析不省略扩展名；
// 配合 tsconfig 的 `allowImportingTsExtensions: true`，同一行对 tsc 与 Node 都成立。
// 写成 `@/lib/labels` 或 `./labels` 都会让 api.test.mts 直接 ERR_MODULE_NOT_FOUND。
import { PRICE_MISSING_LABEL } from "./labels.ts";

export interface PriceRange {
  currency: string;
  type: string;
  min: number | null;
  max: number | null;
}

export interface SalesSource {
  id: number | null;
  name: string | null;
}

export interface HomeCard {
  rank: number;
  series_id: number;
  series_name: string;
  brand_id: number;
  brand_name: string;
  /**
   * 车系名里是否**已经**带上了品牌标识——由后端判定，前端不要再拼一次。
   *
   * 此前三处卡片（`CarCard` / `BrowseCard` / `SearchBar`）各自重推了一遍
   * `!series_name.startsWith(brand_name)`，那条规则只挡**完全**前缀，
   * 于是 33 个车系显示成「小米汽车小米SU7」「启源长安启源A06」「本田东风本田S7」。
   * 判据在 TS 里再写一份必然与后端 `_brand_leads_series` 漂移，故改为直接用后端结论。
   */
  show_brand_prefix: boolean;
  thumbnail_url: string | null;
  body_type: string | null;
  energy_types: string[];
  month: string;
  sales_count: number;
  sales_type: string;
  price_range: PriceRange;
  source: SalesSource;
  data_updated_at: string | null;
  price_range_note: string | null;
}

export interface BrandRef {
  id: number;
  name: string;
}

export interface LatestSales {
  month: string | null;
  sales_count: number | null;
  sales_type: string | null;
  source_name: string | null;
}

export interface ModelYearOut {
  id: number;
  year_name: string;
  launch_status: string;
}

export interface ExternalLink {
  /** official = 品牌官网车型页；source = 数据来源页（官方链接缺失时的兜底，不冒充官网）。 */
  kind: "official" | "source";
  url: string;
  source_name: string | null;
}

export interface VehicleDetail {
  id: number;
  name: string;
  aliases: string[];
  brand: BrandRef | null;
  body_type: string | null;
  positioning: string | null;
  energy_types: string[];
  official_page_url: string | null;
  /** 唯一跳转入口（官方优先，缺失时回退数据来源）；优先级由后端判定。 */
  external_link: ExternalLink | null;
  thumbnail_url: string | null;
  active_status: string;
  price_range: PriceRange;
  latest_sales: LatestSales;
  model_years: ModelYearOut[];
  data_updated_at: string | null;
  price_range_note: string | null;
}

export interface SpecFactOut {
  category: string;
  fact_key: string;
  label: string;
  value: string;
  unit: string | null;
  cycle: string | null;
  display: string;
}

export interface VariantOut {
  id: number;
  series_id: number;
  model_year_id: number;
  year_name: string | null;
  display_name: string;
  config_version: string;
  powertrain: string;
  drivetrain: string;
  package: string | null;
  energy_type: string;
  body_type: string | null;
  status: string;
  official_price: {
    price_cny: number;
    price_type: string;
    effective_from: string | null;
    effective_to: string | null;
  } | null;
  spec_facts: SpecFactOut[];
}

export interface ComparisonFactOut extends SpecFactOut {}

export interface CompareVariantOut {
  variant_id: number;
  series_id: number;
  series_name: string;
  brand_name: string;
  display_name: string;
  energy_type: string;
  body_type: string | null;
  price_cny: number | null;
  // 对比场景不返回外部跳转入口（官方车型页/来源页只在详情页展示）
  facts: ComparisonFactOut[];
}

/** 能源徽标配色（单一真源）：纯电=蓝、插混=靛、增程=琥珀、油混=绿、燃油=灰。 */
export const ENERGY_CHIP: Record<string, string> = {
  BEV: "bg-ice text-apple ring-apple/12",
  PHEV: "bg-[#f2f1fe] text-[#5e5ce6] ring-[#5e5ce6]/12",
  EREV: "bg-[#fdf6ec] text-[#b25e09] ring-[#b25e09]/12",
  HEV: "bg-[#edf9f1] text-[#1d7d3f] ring-[#1d7d3f]/12",
  ICE: "bg-canvas text-ash ring-black/[0.06]",
};

export const SALES_TYPE_LABELS: Record<string, string> = {
  retail: "零售口径",
  wholesale: "批发口径",
  insurance: "上险口径",
  // 卡片/详情页会把口径放进括号里，标签本身不带括号（避免「（榜单口径（汽车之家））」叠括号）；
  // 来源（汽车之家）在卡片与详情页另有「数据来源」标注
  portal: "榜单口径",
};

// ── Agent──────────────────────────────────────────────
export interface Citation {
  source_id: number | null;
  source_name: string | null;
  label: string;
}

export interface Clarification {
  question: string;
  options: string[];
  missing: string[];
}

/** 推荐/版本罗列卡片：不含官方车型页链接（该数据无法从现有来源获得，
 * Agent 侧不提供任何外部跳转，见 skills/sku-comparison.md）。 */
export interface RecommendedVariant {
  variant_id: number;
  series_id: number;
  series_name: string;
  brand_name: string;
  display_name: string;
  energy_type: string;
  price_cny: number | null;
  score: number;
  matched: string[];
  tradeoffs: string[];
  /**
   * 座位这条硬约束是否真的校验过。false = 用户点名了乘坐人数，但库里查不到这台车
   * 的座位数，因此既没满足也没筛掉（缺数据 ≠ 不满足）。
   * 缺失时按 true 处理：后端未部署该字段的老响应不应凭空多出一排警告。
   */
  seat_verified?: boolean;
}

export interface AgentMessageOut {
  session_id: string;
  need_clarification: boolean;
  clarification: Clarification | null;
  /** L4 软缺口追问：随推荐一起给出的补充问题，**不阻塞**本轮结果。
   *  与 need_clarification/clarification 是「二选一」关系（后者会让前端只渲染
   *  追问、不渲染推荐卡片），所以另开一个字段。 */
  followup?: Clarification | null;
  filters: Record<string, unknown>;
  recommended_series_ids: number[];
  recommended_variants: RecommendedVariant[];
  reasons: string[];
  tradeoffs: string[];
  citations: Citation[];
  explanation: string | null;
}

// ── 账户与收藏───────────────────────────────────────
export interface UserOut {
  id: number;
  email: string | null;
  phone: string | null;
  status: string;
  created_at: string;
}

export interface FavoriteOut {
  vehicle_id: number;
  kind: string;
  series_id: number | null;
  name: string | null;
  brand_name: string | null;
  price_cny: number | null;
  created_at: string;
}

export interface ComparisonDetail {
  id: number;
  variant_ids: number[];
  created_at: string;
  variants: CompareVariantOut[];
  common_params: { category: string; fact_key: string; display: string }[];
}

// ── 全部车型浏览（/vehicles）───────────────────────────────────────────────
export interface VehicleListItem {
  series_id: number;
  series_name: string;
  brand_id: number;
  brand_name: string;
  /** 车系名里是否已带品牌标识；由后端判定，见 `HomeCard.show_brand_prefix`。 */
  show_brand_prefix: boolean;
  brand_type: string | null;
  body_type: string | null;
  energy_types: string[];
  thumbnail_url: string | null;
  price_range: { currency: string; type: string; min: number | null; max: number | null };
  price_range_note: string | null;
  latest_sales: {
    month: string | null;
    sales_count: number | null;
    sales_type: string | null;
    source_name: string | null;
  };
  source_name: string | null;
  data_updated_at: string | null;
}

export interface VehicleList {
  total: number;
  page: number;
  page_size: number;
  items: VehicleListItem[];
}

/** 后端根地址（服务端组件直连；浏览器端走 /api/v1 同源 rewrites）。 */
export const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export async function fetchServerJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BACKEND_URL}${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`后端请求失败：${res.status} ${path}`);
  }
  return res.json() as Promise<T>;
}

/**
 * 取列表 + 命中总数（总数在后端 `X-Total-Count` 响应头）。
 * 用于首页销量榜：接口只返回前 N 名（榜单语义），但页面要如实显示「共 N 款」。
 */
export async function fetchServerListWithTotal<T>(
  path: string,
): Promise<{ items: T[]; total: number }> {
  const res = await fetch(`${BACKEND_URL}${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`后端请求失败：${res.status} ${path}`);
  }
  const items = (await res.json()) as T[];
  const header = res.headers.get("x-total-count");
  const total = header !== null && !Number.isNaN(Number(header)) ? Number(header) : items.length;
  return { items, total };
}

/** 统一数字格式（zh-CN 千分位）：模块级实例，SSR/CSR 输出一致，替代 toLocaleString() 的隐式 locale。 */
const numberFormat = new Intl.NumberFormat("zh-CN");

export function formatCount(value: number): string {
  return numberFormat.format(value);
}

export function formatPrice(value: number | null): string {
  if (value === null || value === undefined) return PRICE_MISSING_LABEL;
  const wan = value / 10000;
  return `${wan.toFixed(2).replace(/\.?0+$/, "")} 万元`;
}

export function formatPriceRange(range: PriceRange): string {
  if (range.min === null && range.max === null) return `官方指导价：${PRICE_MISSING_LABEL}`;
  if (range.min !== null && range.max !== null && range.min !== range.max) {
    return `${formatPrice(range.min)} ~ ${formatPrice(range.max)}`;
  }
  if (range.min !== null && range.max !== null) {
    return formatPrice(range.min); // 单一价格点
  }
  if (range.max !== null) return `最高 ${formatPrice(range.max)}`;
  return `${formatPrice(range.min)} 起`;
}

/**
 * 卡片/详情页的价格展示口径：**区间两端都缺时，回落到库内的 `price_range_note` 文案**。
 *
 * 2026-10-02 抽取。此前这条规则在 `CarCard.tsx`、`BrowseCard.tsx`、
 * `vehicles/[series_id]/page.tsx` 各写了一份**逐字相同**的三元表达式——
 * 三处若漂移，会出现「同一个车系在列表显示 17-27 万、点进详情显示暂无」。
 * 判据是「两端都没有数值」，不是「没有下界」：`max` 单独存在是**开区间**，
 * 必须照常走 `formatPriceRange` 的「最高 X」分支，不能回落。
 */
export function resolvePriceRangeNote(range: PriceRange, note?: string | null): string {
  if (range.min === null && range.max === null && note) return note;
  return formatPriceRange(range);
}

/**
 * 元 → 万元（筛选表单的展示口径）。
 *
 * 必须先 toFixed(2) 再裁尾零：正则 `\.?0+$` 本是为 toFixed 的小数尾零设计的，
 * 直接对 String(v / 10000) 使用会把 100000 的 "10" 再裁掉末位 0 变成 "1"，
 * 价格窗口随之被静默收窄十倍（2026-09-24 修复，回归用例见 api.test.mts）。
 */
export function yuanToWan(yuan: string | null | undefined): string {
  if (!yuan) return "";
  const value = Number(yuan);
  if (!Number.isFinite(value)) return "";
  return (value / 10000).toFixed(2).replace(/\.?0+$/, "");
}

/** 万元 → 元（筛选表单写回 URL 的口径）；空值、非数字与负值返回 null，表示不下发该参数。 */
export function wanToYuan(wan: string): number | null {
  if (!wan) return null;
  const value = Number(wan);
  if (!Number.isFinite(value) || value < 0) return null;
  return Math.round(value * 10000);
}

export const ENERGY_LABELS: Record<string, string> = {
  BEV: "纯电",
  PHEV: "插混",
  EREV: "增程",
  HEV: "油混",
  ICE: "燃油",
};

export const BODY_LABELS: Record<string, string> = {
  sedan: "轿车",
  suv: "SUV",
  mpv: "MPV",
  pickup: "皮卡",
};

export const CATEGORY_LABELS: Record<string, string> = {
  基础信息: "基础信息",
  官方指导价: "官方指导价",
  年款: "年款",
  尺寸: "尺寸",
  座位数: "座位数",
  动力: "动力",
  电池和续航: "电池和续航",
  油耗或能耗: "油耗或能耗",
  驱动形式: "驱动形式",
  底盘: "底盘",
  安全配置: "安全配置",
  智能驾驶: "智能驾驶",
  智能座舱: "智能座舱",
  舒适性: "舒适性",
  外观和内饰: "外观和内饰",
};
