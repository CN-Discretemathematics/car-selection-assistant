"""别名机制与「别名必须写明依据」的纪律（2026-10-06）。

别名是**数据**（`vehicle_series.aliases` / `brands.aliases`），修复走
`tools/fix_series_aliases.py`，不在代码里。因此本文件钉的是两件事：

1. **机制可用**：`resolve_series` / `brands` 确实认别名（若哪天解析层不再读
   `series.aliases`，别名就变成写了没人用的数据——这层断了不会有任何报错）；
2. **纪律**：别名清单里每一条都必须写得出**依据**（哪条真实问句 / 库里叫什么）。
   没有依据的条目会让「别名表」退化成拍脑袋的同义词词典。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.catalog.brands import resolve_brand_mentions
from app.catalog.series_index import resolve_series
from tests.seed import make_brand, make_series, make_source
from tools.fix_series_aliases import BRAND_ALIASES, SERIES_ALIASES


def _seed(db: Session):
    src = make_source(db, name="汽车之家")
    toyota = make_brand(db, name="丰田", source=src)
    peugeot = make_brand(db, name="标致", source=src)
    rav = make_series(db, toyota, name="RAV4荣放", body_type="suv",
                      energy_types=("HEV",), source=src)
    rav.aliases = ["RV4", "rav4"]
    p408 = make_series(db, peugeot, name="标致4008", body_type="suv",
                        energy_types=("ICE",), source=src)
    peugeot.aliases = ["标志"]
    db.commit()
    return rav, p408


def test_series_alias_is_resolvable(db_session: Session):
    """解析层必须真的读 `series.aliases`——否则别名是写了没人用的数据。"""
    rav, _ = _seed(db_session)
    for msg in ("丰田RV4和本田CR-V哪个更值得选", "RV4多少钱", "RAV4荣放多少钱"):
        got = {s.id for s, _ in resolve_series(db_session, msg)}
        assert rav.id in got, f"「{msg}」解析不到 RAV4荣放，解析层可能已不读 aliases"


def test_brand_alias_is_resolvable(db_session: Session):
    """品牌级口语别名（「标志」→「标致」）也要能进画像。"""
    _, p408 = _seed(db_session)
    hints = resolve_brand_mentions(db_session, "只要标志4008")
    assert p408.brand_id in (hints.get("brand_ids") or []), (
        f"「只要标志4008」未解析到标致品牌，实际 {hints}"
    )


def test_alias_does_not_break_canonical_name(db_session: Session):
    """反向保护：登记别名不能把正式写法弄坏。"""
    rav, p408 = _seed(db_session)
    assert {s.id for s, _ in resolve_series(db_session, "RAV4荣放怎么样")} == {rav.id}
    assert {s.id for s, _ in resolve_series(db_session, "标致4008怎么样")} == {p408.id}


def test_every_series_alias_has_written_basis():
    """每条车系别名都必须在源码里有**依据注释**（真实问句 / 库里叫什么）。

    没有依据的别名 = 拍脑袋的同义词；本仓已经有一次教训：
    合成数据里我自己编错了 2 条（标注错误率 ≈9%），系统是对的、我错了。
    """
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "tools" / "fix_series_aliases.py"
    ).read_text(encoding="utf-8")
    for target in SERIES_ALIASES:
        assert f'"{target}"' in src, f"{target} 不在清单源文件里"
    for target, wanted in list(SERIES_ALIASES.items()) + list(BRAND_ALIASES.items()):
        for alias in wanted:
            idx = src.find(f'"{target}"')
            assert idx >= 0, f"清单里找不到 {target}"
            # 依据写在条目**前后**都合法（上方整段注释或行内注释），
            # 故两侧都看——只看一侧会逼着人把注释写在固定位置，反而更容易失真。
            window = src[max(0, idx - 400): idx + 400]
            assert ("真实" in window or "库里" in window or "口语" in window
                    or "误写" in window or "问句" in window), (
                f"别名 {alias}（{target}）附近没有写明依据——"
                "请补一句「哪条真实问句 / 库里叫什么」"
            )
