/** 筛选项目选项（首页与全部车型页共用，避免两处标签漂移）。 */
export interface FilterOption {
  value: string;
  label: string;
}

/** 能源类型（HEV 归燃油侧，与后端口径一致）。 */
export const ENERGY_OPTIONS: FilterOption[] = [
  { value: "", label: "能源：全部" },
  { value: "new_energy", label: "新能源" },
  { value: "fuel", label: "燃油（含油混）" },
  { value: "BEV", label: "纯电" },
  { value: "PHEV", label: "插混" },
  { value: "EREV", label: "增程" },
  { value: "HEV", label: "油混" },
  { value: "ICE", label: "燃油" },
];

/** 车身形式（全部车型页含皮卡）。 */
export const BODY_OPTIONS: FilterOption[] = [
  { value: "", label: "车身：全部" },
  { value: "sedan", label: "轿车" },
  { value: "suv", label: "SUV" },
  { value: "mpv", label: "MPV" },
];

export const BODY_OPTIONS_WITH_PICKUP: FilterOption[] = [
  ...BODY_OPTIONS,
  { value: "pickup", label: "皮卡" },
];

/** 品牌类别（§2.1 范围口径）。 */
export const BRAND_OPTIONS: FilterOption[] = [
  { value: "", label: "品牌：全部" },
  { value: "domestic_nev", label: "国产新能源" },
  { value: "luxury", label: "豪华" },
  { value: "japanese", label: "日系" },
  { value: "american", label: "美系" },
  { value: "german", label: "德系" },
  { value: "other_fuel", label: "其他燃油" },
];

/** 首页（热销榜）排序：只有销量维度。 */
export const HOME_SORT_OPTIONS: FilterOption[] = [
  { value: "desc", label: "销量从高到低" },
  { value: "asc", label: "销量从低到高" },
];

/** 全部车型页排序：销量 / 价格。 */
export const BROWSE_SORT_OPTIONS: FilterOption[] = [
  { value: "sales_desc", label: "销量从高到低" },
  { value: "price_asc", label: "价格从低到高" },
  { value: "price_desc", label: "价格从高到低" },
];
