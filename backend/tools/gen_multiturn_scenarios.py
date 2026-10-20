"""生成多轮场景题库（首批 150 题 = 3 场景 × 50）。

## 为什么用生成器而不是手写 JSON

150 道题若手写，变量分布（品牌 / 价位段 / 车身 / 能源 / 拒绝原因）
必然失衡——50 道题可能只是 5 个场景 × 10 个变体，统计意义≈5。
生成器保证**每题的组合都不同**，且关键变量**分层覆盖**。

这与单轮题库的生成器（`tools/gen_eval_questions.py`）是同一套思路：
分层抽样 + 固定种子（可复现）+ 分布记录。

## 三类场景

1. `reject_then_no_recommend`（50）—— 拒过的那台，下一轮还推不推？
   **同会话**，当前系统可测。
2. `returning_user_memory`（50）—— 回访用户的记忆是否生效？
   **跨会话**，依赖 Phase 1 的持久化身份与偏好记忆 → 先落题面，标 `enabled: false`。
3. `memory_deleted`（50）—— 删掉的记忆是否真的不生效？
   **跨会话**，同上。

## 锚点用字符串

单轮题库的 410 道题里 406 道的 `anchors.series_id` 指向的车与题面说的不是同一台
（库重导入后 id 重排，2026-10-20 实测）。多轮题库**只用车系名**做锚点。

用法：
    python tools/gen_multiturn_scenarios.py --out eval/multiturn.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# ── 变量表 ──────────────────────────────────────────────────────────────────
#
# 取值来自真实库的常见组合（品牌、价位、车身、能源）。
# **不硬编码库内 id**——只用名字，题库在任何库上都能跑。

BRANDS = [
    "比亚迪", "大众", "丰田", "本田", "吉利", "长安", "奇瑞", "长城",
    "五菱", "红旗", "蔚来", "小鹏", "理想", "问界", "埃安", "零跑",
]

#: 预算的「口语描述」——**不能**用作断言词。
#:
#: 回答里预算是这么说的（`engine._priority_phrase` / `_profile_summary`）：
#:     「预算 10-12 万」「预算不超 20 万」「预算 18 万以上」
#: **绝不会说「十万出头」**——那只是本生成器里的内部描述。
#: 断言必须用**回答里真会出现的数字**，否则 50 道题会全挂。
#:
#: 故断言用「预算下限的万位数」这种一定出现的形式。
BUDGETS = [
    (80000, 120000, "10"),
    (120000, 180000, "12"),
    (180000, 250000, "18"),
    (250000, 350000, "25"),
]

BODY_TYPES = [
    ("轿车", "sedan"),
    ("SUV", "suv"),
    ("MPV", "mpv"),
]

ENERGY = [
    ("纯电", "BEV"),
    ("插混", "PHEV"),
    ("增程", "EREV"),
    ("燃油", "ICE"),
]

PASSENGERS = [3, 4, 5]

#: 拒绝理由 → 用户会怎么说。措辞取自真实口语，不做成语。
REJECT_PHRASES = [
    "太贵了",
    "太大了",
    "空间不够",
    "不喜欢这个外观",
    "想要个更省油的",
    "预算超了",
    "这个品牌我不想要",
]

#: 「再推荐」的各种说法
REASK_PHRASES = [
    "再推荐几款",
    "还有别的吗",
    "换个推荐",
    "再看看其他的",
    "继续推荐",
]

#: 首批用于「拒过」的具体车系（真实库里常见、且跨品牌不容易重名）
REJECT_CARS = [
    "星愿", "汉", "海豹", "宋PLUS", "秦PLUS", "卡罗拉", "凯美瑞", "轩逸",
    "思域", "CR-V", "RAV4", "途观L", "H6", "CS75PLUS", "缤越", "缤果",
    "AION Y", "小鹏P7", "蔚来ES6", "理想L7", "问界M7", "零跑C11", "极氪001",
    "Model 3", "Model Y", "小米SU7", "智界S7", "岚图FREE", "深蓝S7",
]


def _recommend_turn(rng: random.Random, brand: str, budget, body_cn: str, energy_cn: str, passengers: int) -> str:
    lo, hi, _desc = budget
    lo_w = lo // 10000
    hi_w = hi // 10000
    return (
        f"预算{lo_w}到{hi_w}万，{brand}的{body_cn}，{energy_cn}的，"
        f"家里{passengers}口人用"
    )


def gen_reject_scenarios(rng: random.Random, count: int) -> list[dict]:
    """场景 1：拒过的那台，下一轮还推不推？"""
    out = []
    for i in range(count):
        brand = rng.choice(BRANDS)
        budget = rng.choice(BUDGETS)
        body_cn, _body = rng.choice(BODY_TYPES)
        energy_cn, _e = rng.choice(ENERGY)
        passengers = rng.choice(PASSENGERS)
        car = rng.choice(REJECT_CARS)
        reason = rng.choice(REJECT_PHRASES)
        reask = rng.choice(REASK_PHRASES)

        turns = [
            {"user": _recommend_turn(rng, brand, budget, body_cn, energy_cn, passengers), "assert": []},
            {"user": f"{car}不要，{reason}", "assert": []},
            {
                "user": reask,
                "assert": [{"type": "not_recommended", "car": car}],
            },
        ]
        out.append(
            {
                "id": f"mt_reject_{i + 1:04d}",
                "scenario": "reject_then_no_recommend",
                "enabled": True,
                "turns": turns,
            }
        )
    return out


def gen_returning_scenarios(rng: random.Random, count: int) -> list[dict]:
    """场景 2：回访用户的记忆是否生效？（跨会话，依赖 Phase 1）"""
    out = []
    for i in range(count):
        brand = rng.choice(BRANDS)
        budget = rng.choice(BUDGETS)
        body_cn, _body = rng.choice(BODY_TYPES)
        energy_cn, _e = rng.choice(ENERGY)
        passengers = rng.choice(PASSENGERS)
        lo_w, hi_w, desc = budget

        out.append(
            {
                "id": f"mt_return_{i + 1:04d}",
                "scenario": "returning_user_memory",
                # 跨会话：需要 Phase 1 的持久化身份与偏好记忆，尚未实现 → 先不启用
                "enabled": False,
                "reason_disabled": "依赖 Phase 1：持久化身份 + 偏好记忆（尚未实现）",
                "turns": [
                    {
                        "user": _recommend_turn(rng, brand, budget, body_cn, energy_cn, passengers),
                        "assert": [],
                        "session": "first",
                    },
                    {
                        # 新会话：回访用户的画像应被自动带回
                        "user": "按我之前说的条件推荐",
                        "assert": [
                            {"type": "mentions", "word": desc},
                        ],
                        "session": "second",
                    },
                ],
            }
        )
    return out


def gen_memory_deleted_scenarios(rng: random.Random, count: int) -> list[dict]:
    """场景 3：删掉的记忆是否真的不生效？（跨会话，依赖 Phase 1）"""
    out = []
    for i in range(count):
        brand = rng.choice(BRANDS)
        budget = rng.choice(BUDGETS)
        body_cn, _body = rng.choice(BODY_TYPES)
        energy_cn, _e = rng.choice(ENERGY)
        passengers = rng.choice(PASSENGERS)
        lo_w, hi_w, desc = budget

        out.append(
            {
                "id": f"mt_delete_{i + 1:04d}",
                "scenario": "memory_deleted",
                "enabled": False,
                "reason_disabled": "依赖 Phase 1：持久化身份 + 记忆删除接口（尚未实现）",
                "turns": [
                    {
                        "user": _recommend_turn(rng, brand, budget, body_cn, energy_cn, passengers),
                        "assert": [],
                        "session": "first",
                    },
                    {"user": "把之前记的预算忘掉", "assert": [], "session": "first"},
                    {
                        # 删掉预算后，下一轮不应再按该预算筛
                        "user": "现在推荐一台车",
                        "assert": [
                            {"type": "not_mentions", "word": desc},
                        ],
                        "session": "second",
                    },
                ],
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成多轮场景题库")
    parser.add_argument("--out", default="eval/multiturn.json", help="输出路径")
    parser.add_argument("--count", type=int, default=50, help="每个场景多少题")
    parser.add_argument("--seed", type=int, default=20261020, help="随机种子（固定以保证可复现）")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    scenarios = (
        gen_reject_scenarios(rng, args.count)
        + gen_returning_scenarios(rng, args.count)
        + gen_memory_deleted_scenarios(rng, args.count)
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "seed": args.seed,
                "scenarios": scenarios,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    enabled = sum(1 for s in scenarios if s.get("enabled"))
    print(f"已生成 {len(scenarios)} 个场景 -> {out}")
    print(f"  启用 {enabled}，待启用 {len(scenarios) - enabled}（依赖 Phase 1）")

    # 记录变量分布，便于人工核查覆盖度。
    # ⚠️ 品牌数从**生成时的输入**统计，不要从题面文本里 split——
    # 题面是「预算18到25万，埃安的轿车，燃油的，...」，按「的」拆会得到垃圾。
    brands = set()
    cars = set()
    for s in scenarios:
        for t in s["turns"]:
            u = t["user"]
            if "不要" in u:
                cars.add(u.split("不要")[0])
        brands.add(s["turns"][0]["user"].split("，")[1].split("的")[0])
    print(f"  品牌覆盖 {len(brands)} 个，车系覆盖 {len(cars)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
