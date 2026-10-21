/**
 * 「记住的偏好」的**人话渲染**（2026-10-21）。
 *
 * ## 为什么单独一个 module
 *
 * 这条映射会被两处用（列表项文案、撤回后的提示），而「把后端存的字符串说成人话」
 * 的规则要**只写一遍**——两处各写一遍必然漂移（同一个事写多遍必然漂移）。
 *
 * ## 数据形状来自哪里
 *
 * 后端 `app/agent/preferences.py::TRACKED_KEYS`：
 * 列表类键（usage / body_type / energy_preference / avoid / brand_exclude）存
 * **JSON 数组字符串**；人数存十进制字符串；预算存 `{"min":…, "max":…}`（单位：元）。
 * ⇒ 这里**按值的样子解析**，不假设它一定是 JSON（解析失败就原样显示，
 * 不猜、不补 0——与全站「缺失要显式」的口径一致）。
 */
import { ENERGY_LABELS, formatPrice, type PreferenceMemory } from "./api.ts";

/** 键 → 中文标签。键来自后端 `TRACKED_KEYS`（后端加键时这里要跟）。 */
const KEY_LABELS: Record<string, string> = {
  budget: "预算",
  passengers: "用车人数",
  usage: "用途",
  body_type: "车型类别",
  energy_preference: "能源偏好",
  avoid: "不想要",
  brand_exclude: "不想要的品牌",
};

/** 车身类型 → 中文（后端 `app/common/enums.py::BODY_TYPES`）。 */
const BODY_LABELS: Record<string, string> = {
  sedan: "轿车",
  suv: "SUV",
  mpv: "MPV",
  pickup: "皮卡",
};

const ENERGY_ALIASES: Record<string, string> = {
  new_energy: "新能源",
  fuel: "燃油",
};

/** 把一个枚举值说成人话：先看是不是能源，再看是不是车身，都不是就原样。 */
function humanize(value: string): string {
  if (value in ENERGY_ALIASES) return ENERGY_ALIASES[value];
  if (value in ENERGY_LABELS) return ENERGY_LABELS[value];
  if (value in BODY_LABELS) return BODY_LABELS[value];
  return value;
}

/** JSON 数组字符串 → 字符串数组；解析不出来就当成单值（不猜）。 */
function asList(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed.map((v) => String(v));
  } catch {
    /* 不是 JSON ⇒ 落下面 */
  }
  return raw ? [raw] : [];
}

function asNumber(raw: unknown): number | null {
  const value = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** 一条记忆的**一句话描述**（用户看到的全部内容）。 */
export function describePreference(memory: PreferenceMemory): string {
  const label = KEY_LABELS[memory.key] ?? memory.key;
  switch (memory.key) {
    case "budget": {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(memory.value);
      } catch {
        /* 不是 JSON ⇒ 落下面 */
      }
      const obj = (parsed ?? {}) as { min?: unknown; max?: unknown };
      const min = asNumber(obj.min);
      const max = asNumber(obj.max);
      if (min !== null && max !== null) return `${label}：${formatPrice(min)} ~ ${formatPrice(max)}`;
      if (max !== null) return `${label}：不超过 ${formatPrice(max)}`;
      if (min !== null) return `${label}：${formatPrice(min)} 起`;
      return `${label}：${memory.value}`;
    }
    case "passengers":
      return `${label}：${memory.value} 人`;
    case "usage":
    case "body_type":
    case "energy_preference":
    case "avoid":
    case "brand_exclude": {
      const items = asList(memory.value).map(humanize).filter(Boolean);
      return items.length ? `${label}：${items.join("、")}` : `${label}：${memory.value}`;
    }
    default:
      return `${label}：${memory.value}`;
  }
}
