/** 后端 API 类型与访问辅助（的响应结构）。 */

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

export interface VehicleDetail {
  id: number;
  name: string;
  aliases: string[];
  brand: BrandRef | null;
  body_type: string | null;
  positioning: string | null;
  energy_types: string[];
  official_page_url: string | null;
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
  official_page_url: string | null;
  facts: ComparisonFactOut[];
}

export const SALES_TYPE_LABELS: Record<string, string> = {
  retail: "零售口径",
  wholesale: "批发口径",
  insurance: "上险口径",
  portal: "门户榜单口径（汽车之家）",
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
  official_page_url: string | null;
}

export interface AgentMessageOut {
  session_id: string;
  need_clarification: boolean;
  clarification: Clarification | null;
  filters: Record<string, unknown>;
  recommended_series_ids: number[];
  recommended_variants: RecommendedVariant[];
  reasons: string[];
  tradeoffs: string[];
  citations: Citation[];
  official_links: string[];
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

export function formatPrice(value: number | null): string {
  if (value === null || value === undefined) return "暂无";
  const wan = value / 10000;
  return `${wan.toFixed(2).replace(/\.?0+$/, "")} 万元`;
}

export function formatPriceRange(range: PriceRange): string {
  if (range.min === null && range.max === null) return "官方指导价：暂无";
  if (range.min !== null && range.max !== null && range.min !== range.max) {
    return `${formatPrice(range.min)} ~ ${formatPrice(range.max)}`;
  }
  if (range.min !== null && range.max !== null) {
    return formatPrice(range.min); // 单一价格点
  }
  if (range.max !== null) return `最高 ${formatPrice(range.max)}`;
  return `${formatPrice(range.min)} 起`;
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
