"""多轮题库分布审计的守卫（计划 §七）。

审计工具本身也要被守住：口径解析错了，结论就是错的。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import audit_multiturn_corpus as audit  # noqa: E402
from tests.seed import make_brand, make_series, make_source, make_variant, make_year  # noqa: E402


def test_requirement_sentence_is_parsed():
    """第一轮需求句：价位段 / 品牌 / 车身 / 能源 / 人数。"""
    parsed = audit._parse_turn1("预算18到25万，埃安的轿车，燃油的，家里3口人用")
    assert parsed["price_band"] == "18-25万", parsed
    assert parsed["brand"] == "埃安", parsed
    assert parsed["body"] == "轿车", parsed
    assert parsed["energy"] == "燃油", parsed
    assert parsed["people"] == "3口", parsed


def test_reject_reason_classes():
    """拒绝理由的 6 类（实测题库里的 42 种拒绝句归纳出来的）。"""
    cases = {
        "凯美瑞不要，太贵了": "价格",
        "Model 3不要，预算超了": "价格",
        "问界M7不要，这个品牌我不想要": "品牌",
        "海豹不要，不喜欢这个外观": "外观",
        "AION Y不要，想要个更省油的": "能耗",
        "智界S7不要，空间不够": "空间",
        "理想L7不要，太大了": "尺寸",
        "说不清理由": "未归类",
    }
    for text, expected in cases.items():
        assert audit._parse_reason(text) == expected, (text, expected)


def test_audit_reports_the_thinnest_stratum():
    """审计要报出「价位段 × 拒绝理由」的最小格题数——某一格只有 1 题时，
    按该格统计就没有统计意义（计划 §3.3 的原话：「否则 50 题的统计意义≈5 题」）。"""
    corpus = {
        "scenarios": [
            {
                "id": f"q{i}",
                "scenario": "s",
                "enabled": True,
                "turns": [
                    {"user": f"预算{band}，{brand}的轿车，燃油的，家里3口人用", "assert": []},
                    {"user": f"某车不要，{reason}", "assert": []},
                ],
            }
            for i, (band, brand, reason) in enumerate(
                [
                    ("8到12万", "吉利", "太贵了"),
                    ("8到12万", "吉利", "太贵了"),
                    ("18到25万", "埃安", "太贵了"),
                    ("18到25万", "埃安", "空间不够"),
                ]
            )
        ]
    }
    report = audit.audit(corpus)
    body = report["scenarios"]["s"]
    assert body["count"] == 4, body
    assert body["min_cell"] == 1, body
    assert body["min_cell_count"] == 3, body
    assert body["variables"]["price_band"] == {"8-12万": 2, "18-25万": 2}, body
    assert body["reject_reason"] == {"价格": 3, "空间": 1}, body


def test_enabled_flag_is_reported():
    """`enabled=False` 的场景今天**跑不了**（依赖尚未上线的能力）——
    报告必须把它单独点出来，否则「150 题」会被当成「150 题可跑」。"""
    corpus = {
        "scenarios": [
            {
                "id": "a",
                "scenario": "s",
                "enabled": False,
                "turns": [{"user": "预算8到12万，吉利的轿车，燃油的，家里3口人用", "assert": []}],
            }
        ]
    }
    body = audit.audit(corpus)["scenarios"]["s"]
    assert body["enabled"] == {"False": 1}, body


def test_unsafe_assert_is_detected(db_session):
    """`not_mentions '12'` 若答案里合法地出现「12.99 万」，这条断言会**误伤正确答案**
    （假否定 ⇒ 低估「记忆删除」能力）。审计必须把它点出来。"""
    source = make_source(db_session, name="审计测试来源")
    brand = make_brand(db_session, name="审计品牌", source=source)
    series = make_series(
        db_session, brand, name="审计车系", body_type="sedan",
        energy_types=("BEV",), source=source,
    )
    year = make_year(db_session, series)
    # 12-18 万 档里存在 12.99 万的车 → 答案里合法地出现「12」
    make_variant(
        db_session, series, year, config_version="标准版", energy_type="BEV",
        price_cny="129900", source=source,
    )
    db_session.commit()

    corpus = {
        "scenarios": [
            {
                "id": "mt_delete_0001",
                "scenario": "memory_deleted",
                "enabled": False,
                "turns": [
                    {"user": "预算12到18万，丰田的MPV，燃油的，家里3口人用", "assert": []},
                    {"user": "把之前记的预算忘掉", "assert": []},
                    {
                        "user": "现在推荐一台车",
                        "assert": [{"type": "not_mentions", "word": "12"}],
                    },
                ],
            }
        ]
    }
    bands = audit._bands_from_corpus(corpus)
    prices = audit._prices_by_band(db_session, bands)
    result = audit.check_asserts(corpus, prices)
    assert result["checked"] == 1, result
    assert len(result["unsafe_questions"]) == 1, result
    assert result["unsafe_questions"][0]["forbidden_word"] == "12", result
    assert 129900.0 in result["unsafe_questions"][0]["colliding_prices"], result
