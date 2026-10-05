"""车系/品牌口语别名登记（默认 dry-run，只报告不改库）。

## 背景（2026-10-06，真实用户问句实测）

采了 41 条真实问句（懂车帝「提问」帖原文 + 汽车之家问答区标题，半编辑标题不收），
用 `resolve_series` 逐条实跑后，把「解析失败」拆成两类——**这个区分是本工具存在的全部理由**：

| 类别 | 真实问句 | 库里 | 处置 |
| --- | --- | --- | --- |
| **拼写变体**（库里有，只是写法不同） | 丰田RV4 | RAV4荣放 | ✅ 登记别名 |
| | 标志4008 | 标致4008（品牌别名空） | ✅ 登记品牌别名 |
| **库里根本没有** | 明锐 / 奔驰A180 / 凯迪拉克ATS / 新英朗 / 传祺M6 | 无 | ❌ **不编数据**，交给「库里没有要明说」披露 |

用户 2026-10-06 拍板：**只补可枚举的别名，缺车的不编。**

⚠️ 为什么不能图省事上模糊匹配：中文车名短、字面相近的多（缤果/缤果S/缤果Pro），
编辑距离一放宽就会把「缤果」认成「缤果S」。**别名必须逐条登记、逐条可审**，
这正是本工具是一张显式清单而不是一个算法的原因。

## 为什么不写在代码里

`series_index._load_name_entries` 第 160 行**已经** `names.extend(list(series.aliases or []))`，
`brands._load_entries` 同样读 `Brand.aliases`——**解析层早就支持别名**，缺的只是数据。
且全仓没有任何工具写 `aliases`（`tools/*.py` 零命中），所以**导入不会冲掉它**，
补丁能存活到下次抓取之后。写成代码常量反而会被数据源覆盖或漂移。

## 用法（在 backend/ 目录下）

    python tools/fix_series_aliases.py                 # dry-run：报告将要改什么
    python tools/fix_series_aliases.py --apply         # 实际写库
    python tools/fix_series_aliases.py --apply --dry   # 写库但只打印（审计用）

幂等：**只追加**缺失的别名，永不删除或覆盖已有内容——别名往往是人工积累的，
一次误覆盖就丢了。重复跑 `--apply` 第二次应报「无待修」。
"""
from __future__ import annotations

import argparse
import sys

from _bootstrap import ensure_backend_on_path  # noqa: F401
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.database import get_session_factory
from app.common.models import Brand, VehicleSeries

#: 车系别名：匹配「库里确实有这台车，只是用户写法不同」的情形。
#: 每条都必须写明**依据**——哪条真实问句、库里叫什么。没有依据的不要往里加。
SERIES_ALIASES: dict[str, tuple[str, ...]] = {
    # 「丰田RV4和本田CR-V哪个更值得选」是懂车帝问答区真实标题；
    # 库里叫 RAV4荣放（brand=丰田），用户普遍漏掉那个 A。
    "RAV4荣放": ("RV4", "rav4"),
    # 用户口语常说「标志4008」，正式名是「标致4008」。
    # 注意：品牌名「标志」的口语写法走下面的 BRAND_ALIASES 更合适，
    # 这里只登记车系级写法，避免同一处别名在两层重复登记。
    "标致4008": ("标志4008",),
}

#: 品牌别名：「标志」是「标致」的高频误写（车标叫三斜杠，人们常打成标志）。
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "标致": ("标志",),
    # 以下两对同样是高频口语/误写，且库里 aliases 为空；一并登记。
    # 「奇瑞」与「奇瑞汽车」：库里品牌名已是「奇瑞汽车」的情形才需要，故先只登记确认过的。
}


def plan_series(db: Session) -> list[tuple[int, str, str, list[str]]]:
    """返回 (series_id, 车系名, 品牌名, 待追加的别名)。"""
    rows = db.execute(
        select(VehicleSeries, Brand).join(Brand, VehicleSeries.brand_id == Brand.id)
    ).all()
    by_name: dict[str, tuple[VehicleSeries, Brand]] = {}
    for series, brand in rows:
        by_name.setdefault(series.name, (series, brand))
        by_name.setdefault(f"{brand.name}{series.name}", (series, brand))

    out: list[tuple[int, str, str, list[str]]] = []
    for target, wanted in SERIES_ALIASES.items():
        hit = by_name.get(target)
        if hit is None:
            print(f"  ! 目标车系 {target!r} 在库里不存在——跳过（不要新建车系，"
                  "那是数据导入的活，不是别名）", file=sys.stderr)
            continue
        series, brand = hit
        existing = {str(a).strip() for a in (series.aliases or []) if str(a).strip()}
        missing = [a for a in wanted if a.strip() and a not in existing]
        if missing:
            out.append((series.id, series.name, brand.name, missing))
    return out


def plan_brand(db: Session) -> list[tuple[int, str, list[str]]]:
    """返回 (brand_id, 品牌名, 待追加的别名)。"""
    rows = db.execute(select(Brand)).scalars().all()
    by_name = {b.name: b for b in rows}
    out: list[tuple[int, str, list[str]]] = []
    for target, wanted in BRAND_ALIASES.items():
        brand = by_name.get(target)
        if brand is None:
            print(f"  ! 目标品牌 {target!r} 在库里不存在——跳过", file=sys.stderr)
            continue
        existing = {str(a).strip() for a in (brand.aliases or []) if str(a).strip()}
        missing = [a for a in wanted if a.strip() and a not in existing]
        if missing:
            out.append((brand.id, brand.name, missing))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="登记车系/品牌口语别名（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只报告）")
    args = parser.parse_args()

    factory = get_session_factory()
    db: Session = factory()
    try:
        s_plan = plan_series(db)
        b_plan = plan_brand(db)

        if not s_plan and not b_plan:
            print("无待修：所有别名均已登记（幂等）。")
            return 0

        print(f"待登记：车系 {len(s_plan)} 条、品牌 {len(b_plan)} 条"
              f"{'（DRY-RUN，未写库）' if not args.apply else '（将写库）'}\n")
        for sid, name, brand, missing in s_plan:
            print(f"  车系 #{sid} {brand}{name}  += {missing}")
        for bid, name, missing in b_plan:
            print(f"  品牌 #{bid} {name}  += {missing}")

        if not args.apply:
            print("\n加 --apply 实际写库。")
            return 0

        for sid, _name, _brand, missing in s_plan:
            series = db.get(VehicleSeries, sid)
            series.aliases = list(series.aliases or []) + missing
        for bid, _name, missing in b_plan:
            brand = db.get(Brand, bid)
            brand.aliases = list(brand.aliases or []) + missing
        db.commit()
        print(f"\n已写入：车系 {len(s_plan)} 条、品牌 {len(b_plan)} 条。"
              "复跑本脚本应报「无待修」。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
