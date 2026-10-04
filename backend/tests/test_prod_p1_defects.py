"""生产实测抓到的 4 个 P1 缺陷的回归测试（2026-10-05）。

每条用例的断言值都取自站点实拍文案，不是凭空构造。
"""
from __future__ import annotations

from app.agent.engine import (
    AgentEngine,
    _brand_series,
    _clip_evidence,
    _mentions_variant,
    _variant_label,
    next_clarification,
)
from app.agent.schemas import Budget, UserProfile


# ── P1-1 品牌与车系名叠词 ────────────────────────────────────────────────────
def test_brand_series_dedupes_prefix():
    """实拍原句：「捷途 捷途旅行者C-DM」——车系名自带品牌前缀，不能再拼一次。"""
    v = {"brand_name": "捷途", "series_name": "捷途旅行者C-DM", "display_name": ""}
    assert _brand_series(v) == "捷途旅行者C-DM"


def test_brand_series_keeps_distinct_parts():
    """品牌与车系名确实不同时，两个都要（别把去重做成截断）。"""
    v = {"brand_name": "方程豹", "series_name": "钛7", "display_name": ""}
    assert _brand_series(v) == "方程豹 钛7"


def test_variant_label_does_not_repeat_brand_in_display_name():
    v = {
        "brand_name": "捷途",
        "series_name": "捷途旅行者C-DM",
        "display_name": "捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座",
    }
    label = _variant_label(v)
    assert label.count("捷途") == 1, f"品牌叠词：{label}"
    assert "PLUS 211km XWD 征服 5座" in label


# ── P1-2 佐证截断不得腰斩 ────────────────────────────────────────────────────
def test_clip_evidence_cuts_at_punctuation_not_mid_word():
    """实拍原句：「…核心参数与配置：级别 = 紧…」。"""
    raw = "捷途旅行者C-DM 2026款 129km 畅行版 5座（插电混动，指导价 20.99 万）核心参数与配置：级别 = 紧凑型 SUV。"
    clipped = _clip_evidence(raw)
    assert not clipped.endswith("= 紧…"), f"仍在腰斩：{clipped}"
    assert clipped.endswith("…")
    assert len(clipped) <= 62


def test_clip_evidence_keeps_short_text_intact():
    raw = "指导价 20.99 万"
    assert _clip_evidence(raw) == raw


# ── P1-3 佐证必须点名推荐的那个款型 ──────────────────────────────────────────
def test_mentions_variant_accepts_same_variant():
    """实拍：推荐的正是 PLUS 211km XWD 征服 5座，佐证提到它 → 应判为同一款。"""
    v = {"display_name": "捷途 捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座"}
    text = "捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座，插电混动，指导价 19.79 万。"
    assert _mentions_variant(text, v)


def test_mentions_variant_rejects_sibling_variant():
    """实拍原句：推荐 PLUS 211km，佐证却是同车系的 129km 畅行版 → 必须拒绝。

    这条若失效，生产上就会把 20.99 万（**超出用户 20 万预算**）的款型参数
    当成所推荐那台的佐证，与同一段里的「均在 20 万元预算内」自相矛盾。
    """
    v = {"display_name": "捷途 捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座"}
    text = "捷途旅行者C-DM 2026款 129km 畅行版 5座（插电混动，指导价 20.99 万）核心参数与配置：级别 = 紧凑型 SUV。"
    assert not _mentions_variant(text, v)


def test_mentions_variant_rejects_series_level_text():
    """只有车系级介绍、没有点名款型的片段不能充当某一款的佐证。"""
    v = {"display_name": "捷途 捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座"}
    assert not _mentions_variant("捷途旅行者C-DM 是一款插电混动紧凑型 SUV。", v)


# ── P1-4 追问不得预设一辆还不存在的车 ────────────────────────────────────────
def test_usage_clarification_does_not_presuppose_a_car():
    """实拍：用户只说「20万预算」，从没提车，却被问「**这辆车**的主要用途是什么呢？」。"""
    profile = UserProfile(budget=Budget(max=200000))
    out = next_clarification(profile)
    assert out is not None
    assert "这辆车" not in out.question, f"预设了不存在的车：{out.question}"


def test_usage_clarification_options_still_present():
    """去指代不能连累选项——chip 是一路都在的交互。"""
    profile = UserProfile(budget=Budget(max=200000))
    out = next_clarification(profile)
    assert out is not None and out.options == ["上下班通勤", "家庭出行", "长途自驾", "商务接待"]


# ── 接线测试：_collect_evidence 真的用了款型过滤 ─────────────────────────────
# 首轮反向验证发现：只测 `_mentions_variant` 这个纯函数是不够的——把**调用点**
# 换回「取第一条命中」，全套测试照样全绿。纯函数的测试抓不到「接线被绕过」，
# 所以这里直接钉住 `_collect_evidence` 的实际行为。
class _FakeEngine:
    _collect_evidence = AgentEngine._collect_evidence


def test_collect_evidence_rejects_sibling_variant(db_session, monkeypatch):
    """实拍：推荐 PLUS 211km（19.79 万），佐证却是同车系的 129km 畅行版（20.99 万）。

    20.99 万**超出用户给的 20 万预算**，与同一段话里的「均在 20 万元预算内」
    当场打架。返回空 = 不写佐证，是唯一正确的结果。
    """
    hits = [{
        "text": "捷途旅行者C-DM 2026款 129km 畅行版 5座（插电混动，指导价 20.99 万）"
                "核心参数与配置：级别 = 紧凑型 SUV。",
        "kind": "spec",
        "source_url": None,
    }]
    monkeypatch.setattr("app.agent.engine.retrieval_search", lambda *a, **k: hits)
    top = [{
        "variant_id": 1, "series_id": 7,
        "series_name": "捷途旅行者C-DM",
        "display_name": "捷途 捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座",
    }]
    profile = UserProfile()
    profile.usage = ["家庭"]
    assert _FakeEngine()._collect_evidence(db_session, profile, top) == []


def test_collect_evidence_keeps_matching_variant(db_session, monkeypatch):
    """同一款型被点名时佐证必须留下——不能为了修缺陷把佐证功能整体关掉。"""
    hits = [{
        "text": "捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座，插电混动，指导价 19.79 万。",
        "kind": "spec",
        "source_url": None,
    }]
    monkeypatch.setattr("app.agent.engine.retrieval_search", lambda *a, **k: hits)
    top = [{
        "variant_id": 1, "series_id": 7,
        "series_name": "捷途旅行者C-DM",
        "display_name": "捷途 捷途旅行者C-DM 2026款 PLUS 211km XWD 征服 5座",
    }]
    profile = UserProfile()
    profile.usage = ["家庭"]
    out = _FakeEngine()._collect_evidence(db_session, profile, top)
    assert len(out) == 1 and "19.79" in out[0]["text"]
