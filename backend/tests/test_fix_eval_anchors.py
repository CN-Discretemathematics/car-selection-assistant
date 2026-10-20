"""`fix_eval_anchors` 的自测：判据本身要被钉住。

本仓的教训（「不可能变红」的测试、变异测试）：
**只测「跑起来不报错」等于没测**。所以这里测的是**判据**：

1. 漂移能被**识别**（不是所有题都判 ok）
2. 修复后 anchors **真的**与题面一致
3. **无实体标识的题不被瞎猜**——锚点原样保留，状态标 unlocatable
4. **幂等**：修完再修，改动为 0
5. **不越权**：只改 anchors，**题面一字不动**
6. 退出码语义：--check 有漂移 → 1；无漂移 → 0
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.common.models import VehicleSeries, VehicleVariant  # noqa: F401
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fix_eval_anchors import (  # noqa: E402
    analyze_all,
    apply_fixes,
    load_entities,
)


@pytest.fixture()
def seeded(db_session: Session):
    """造一个「有车系名」「无车系名」两类题面各一道。"""
    source = make_source(db_session)
    brand = make_brand(db_session, name="测试品牌甲", source=source)
    series = make_series(db_session, brand, name="甲车系", source=source)
    year = make_year(db_session, series)
    variant = make_variant(
        db_session, series, year, config_version="标准版", source=source
    )
    db_session.commit()
    return db_session, series, variant


def _write_questions(tmp_path: Path, questions: list[dict]) -> Path:
    p = tmp_path / "questions.json"
    p.write_text(json.dumps({"questions": questions}, ensure_ascii=False), encoding="utf-8")
    return p


# ── 1. 漂移能被识别 ────────────────────────────────────────────────────────


def test_stale_anchor_is_detected(seeded):
    """anchors 指向别的车系 → 判 stale，并给出重定位结果。"""
    db, series, _variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "parameter",
            "text": "甲车系 2025款 标准版 的座位数是多少",
            # 故意指向一个错的车系 id
            "anchors": {"series_id": 999999, "variant_id": 999999},
        }
    ]
    results, stats = analyze_all(db, questions)
    assert stats["stale"] == 1
    res = results[0]
    assert res["new_series_id"] == series.id
    assert res["new_series_name"] == "甲车系"


def test_correct_anchor_is_ok(seeded):
    """anchors 已与题面一致 → 判 ok，不动。"""
    db, series, variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "parameter",
            "text": "甲车系 2025款 标准版 的座位数是多少",
            "anchors": {"series_id": series.id, "variant_id": variant.id},
        }
    ]
    _results, stats = analyze_all(db, questions)
    assert stats["ok"] == 1
    assert stats["stale"] == 0


# ── 2. 无实体标识的题不被瞎猜 ──────────────────────────────────────────────


def test_unlocatable_question_is_not_guessed(seeded):
    """题面只有约束、没有实体名 → 判 unlocatable，**锚点原样保留**。

    这是本工具最重要的纪律：**不猜**。
    猜错 = 把一道题的锚点指向一台完全不相干的车，比漂移更难发现。
    """
    db, _series, _variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "recommend",
            "text": "预算21万，想要一台纯电SUV，家里5口人用，有推荐吗",
            "anchors": {"series_id": 123456, "variant_id": 654321},
        }
    ]
    results, stats = analyze_all(db, questions)
    assert stats["unlocatable"] == 1
    assert "new_series_id" not in results[0]

    apply_fixes(questions, results)
    assert questions[0]["anchors"]["series_id"] == 123456, "不该被改"
    assert questions[0]["anchors"]["variant_id"] == 654321, "不该被改"


# ── 3. 修复后 anchors 真的与题面一致 ───────────────────────────────────────


def test_fix_makes_anchor_match_text(seeded):
    """修复后，anchors 指向的车系名**出现在题面里**。"""
    db, series, _variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "parameter",
            "text": "甲车系 2025款 标准版 的座位数是多少",
            "anchors": {"series_id": 999999, "variant_id": 999999},
        }
    ]
    results, _stats = analyze_all(db, questions)
    fixed, _changed = apply_fixes(questions, results)
    assert fixed == 1
    assert questions[0]["anchors"]["series_id"] == series.id


def test_fix_is_idempotent(seeded):
    """修完再修，改动为 0。"""
    db, _series, _variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "parameter",
            "text": "甲车系 2025款 标准版 的座位数是多少",
            "anchors": {"series_id": 999999, "variant_id": 999999},
        }
    ]
    results, _stats = analyze_all(db, questions)
    apply_fixes(questions, results)
    after_first = json.dumps(questions, ensure_ascii=False, sort_keys=True)

    results2, stats2 = analyze_all(db, questions)
    fixed2, _ = apply_fixes(questions, results2)
    assert fixed2 == 0, "第二次修不该有任何改动"
    assert stats2["stale"] == 0
    assert json.dumps(questions, ensure_ascii=False, sort_keys=True) == after_first


# ── 3b. 另外两种「不能瞎处理」的状态 ──────────────────────────────────────


def test_question_without_anchor_is_left_alone(seeded):
    """**没有实体锚的题（semantic 桶）不被碰。**

    这类题的 anchors 本就是空的——它判的是「回答有没有提到目标实体」，
    没有目标实体就没有锚。工具必须原样放过，**不能给它硬造一个锚**。
    """
    db, _series, _variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "semantic",
            "text": "帮我推荐一台油电混动的车，轻客",
            "anchors": {},
        }
    ]
    results, stats = analyze_all(db, questions)
    assert stats["no_anchor"] == 1
    assert results[0]["status"] == "no_anchor"

    apply_fixes(questions, results)
    assert questions[0]["anchors"] == {}, "无锚点的题不该被加锚点"


def test_ambiguous_question_is_reported_not_guessed(seeded):
    """题面能定位到**多个**车系 → 判 ambiguous，**不瞎选一个**。

    这是本工具的第二条纪律：多候选时宁可交给人判断，
    也不能任选一个——选错会把一道题指向完全不相干的车。

    造多候选的方式：题面同时提到**两个**车系名 → 解析器给出两个候选。
    """
    db, _series, _variant = seeded
    source2 = make_source(db)
    brand2 = make_brand(db, name="测试品牌乙", source=source2)
    series2 = make_series(db, brand2, name="乙车系", source=source2)
    year2 = make_year(db, series2)
    make_variant(db, series2, year2, config_version="标准版", source=source2)
    db.commit()

    questions = [
        {
            "id": "q0001",
            "bucket": "compare",
            "text": "甲车系 和 乙车系 的座位数分别是多少",
            "anchors": {"series_id": 999999},
        }
    ]
    results, stats = analyze_all(db, questions)
    assert stats["ambiguous"] == 1, f"应判 ambiguous，实得 {dict(stats)}"
    assert len(results[0]["candidates"]) == 2

    apply_fixes(questions, results)
    assert questions[0]["anchors"]["series_id"] == 999999, "多候选时不该改锚点"


def test_apply_fixes_only_touches_stale(seeded):
    """`apply_fixes` 只改 `stale` 的题——别的状态一律放过。"""
    db, _series, _variant = seeded
    questions = [
        {
            "id": "q0001",
            "bucket": "parameter",
            "text": "甲车系 2025款 标准版 的座位数是多少",
            "anchors": {"series_id": 999999, "variant_id": 999999},
        },
        {
            "id": "q0002",
            "bucket": "recommend",
            "text": "预算21万，想要一台纯电SUV，家里5口人用，有推荐吗",
            "anchors": {"series_id": 123456},
        },
    ]
    results, _stats = analyze_all(db, questions)
    fixed, _ = apply_fixes(questions, results)
    assert fixed == 1, "只该修 stale 的那一题"
    assert questions[1]["anchors"]["series_id"] == 123456, "非 stale 的题不该被改"


# ── 4. 不越权：题面一字不动 ───────────────────────────────────────────────


def test_fix_never_touches_text(seeded):
    """**只改 anchors，题面一字不动。**

    增量修的承诺是「保留题面」——题面变了，历史指标就不可比了。
    """
    db, _series, _variant = seeded
    original_text = "甲车系 2025款 标准版 的座位数是多少"
    questions = [
        {
            "id": "q0001",
            "bucket": "parameter",
            "text": original_text,
            "anchors": {"series_id": 999999, "variant_id": 999999},
        }
    ]
    results, _stats = analyze_all(db, questions)
    apply_fixes(questions, results)
    assert questions[0]["text"] == original_text
    # 也不该动 expect（那是断言内容，不是锚点）
    assert "expect" not in questions[0] or questions[0]["expect"] == {}


# ── 5. 退出码语义 ──────────────────────────────────────────────────────────


def test_check_exit_code_semantics(seeded, tmp_path, capsys):
    """--check：有漂移 → 1；无漂移 → 0。"""
    from fix_eval_anchors import main

    db, series, variant = seeded
    # 造一个有漂移的题库
    qpath = _write_questions(
        tmp_path,
        [
            {
                "id": "q0001",
                "bucket": "parameter",
                "text": "甲车系 2025款 标准版 的座位数是多少",
                "anchors": {"series_id": 999999, "variant_id": 999999},
            }
        ],
    )
    rc = main(["--check", "--questions", str(qpath)], session=db)
    assert rc == 1, "有漂移时 --check 应返回 1"

    # 修完后应为 0
    rc = main(["--fix", "--questions", str(qpath)], session=db)
    assert rc == 0
    rc = main(["--check", "--questions", str(qpath)], session=db)
    assert rc == 0, "修完后 --check 应返回 0"


def test_fix_writes_backup(tmp_path, seeded):
    """--fix 必须先备份——改的是评测题库，坏了要能回退。"""
    from fix_eval_anchors import main

    db, series, variant = seeded
    qpath = _write_questions(
        tmp_path,
        [
            {
                "id": "q0001",
                "bucket": "parameter",
                "text": "甲车系 2025款 标准版 的座位数是多少",
                "anchors": {"series_id": 999999, "variant_id": 999999},
            }
        ],
    )
    original = qpath.read_text(encoding="utf-8")
    main(["--fix", "--questions", str(qpath)], session=db)
    backup = Path(str(qpath) + ".bak")
    assert backup.exists(), "必须留备份"
    assert backup.read_text(encoding="utf-8") == original, "备份应是修复前的内容"


# ── 6. 库内实体的稳定标识 ─────────────────────────────────────────────────


def test_load_entities_returns_names(seeded):
    """实体表要给出**稳定字符串标识**（车系名/款型名），供反查。

    `display_name` 是题面里的形态（带年款）；`config_version` 是纯配置名
    （共享面更大，143 个 vs 89 个）——两者都要给出。
    """
    db, series, variant = seeded
    series_names, variants = load_entities(db)
    assert series_names[series.id] == "甲车系"
    _sid, disp, cfg = variants[variant.id]
    assert cfg == "标准版", "config_version 应是原样保存的配置名"
    assert disp == "甲车系 2025款 标准版", "display_name 应是题面形态（车系名+年款+配置名）"
