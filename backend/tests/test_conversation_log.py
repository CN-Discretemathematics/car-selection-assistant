"""对话逐轮留档（`app/agent/conversation_log.py`）。

为什么有这个东西：在此之前全仓**没有保存过任何对话**——`SessionStore` 是进程内
dict（TTL 1 小时），生产 Redis 同样带 TTL。线上答错了无法复现。而本仓的典型缺陷
形态是「静默给出错误答案」（确定性链路里的字符串匹配/解包写错），既不抛异常、
日志无痕，往往也没有用户投诉——唯一能事后发现它的办法就是能取回当时那一轮。

本文件钉住四件事：落档内容够复现、PII 已脱敏、写失败绝不冒泡、测试环境默认不写。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import conversation_log
from app.agent.conversation_log import load_session, purge_expired, record_turn
from app.agent.routing import RouteDecision
from app.agent.schemas import AgentMessageOut, UserProfile
from app.common.config import get_settings
from app.common.models import AgentTurnLog


@pytest.fixture()
def log_enabled(monkeypatch):
    """打开留档。`get_settings` 带 lru_cache，必须先清缓存再改环境变量——
    否则本机上一次别的测试调过一次，值就固定住了。"""
    monkeypatch.setenv("AGENT_CONVERSATION_LOG_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _out(text: str = "比亚迪汉在售 20 款。") -> AgentMessageOut:
    return AgentMessageOut(session_id="s1", explanation=text)


def _decision(intent: str = "series_qa", rule: str = "series_qa:resolved+should_answer"):
    return RouteDecision(intent=intent, matched_rule=rule, signals={"resolved_count": 1})


# ── 默认关闭（测试环境不得写库）────────────────────────────────────────────

def test_disabled_by_default_in_tests():
    """conftest 显式置 false。这条是那道闸的回归——没有它，将来有人把默认值
    改成 true 就会让每个 agent 测试都开始往库里写行。"""
    assert conversation_log.is_enabled() is False


def test_record_is_noop_when_disabled(db_session: Session):
    record_turn(
        db_session, session_id="s-disabled", user_text="汉怎么样",
        out=_out(), decision=_decision(),
    )
    assert db_session.query(AgentTurnLog).count() == 0


# ── 落档内容够复现 ────────────────────────────────────────────────────────

def test_records_minimum_replay_set(log_enabled, db_session: Session):
    """复现一���需要：输入、决策、命中的规则、解析出的车系、回复、画像。
    少任何一列，「拿线上那一轮重放并比对」这件事就做不成。"""
    series = type("S", (), {"id": 7})()
    record_turn(
        db_session,
        session_id="s-1",
        user_text="汉的续航是多少",
        out=_out("汉的核心参数（全系极值）：续航 605 km。"),
        decision=_decision(),
        resolved=[(series, None)],
        profile=UserProfile(),
    )
    rows = load_session(db_session, "s-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_text"] == "汉的续航是多少"
    assert row["intent"] == "series_qa"
    assert row["matched_rule"] == "series_qa:resolved+should_answer"
    assert row["series_ids"] == [7]
    assert "605" in row["reply_text"]
    assert row["profile_snapshot"] is not None
    assert row["turn_index"] == 0


def test_turn_index_increments_per_session(log_enabled, db_session: Session):
    """turn_index 按「本会话已有留档条数」算，不用会话存储——后者有 TTL 会清空，
    两者会错位，用哪个就会在长会话里把轮次编号排错。"""
    for i in range(3):
        record_turn(
            db_session, session_id="s-x", user_text=f"第{i}轮",
            out=_out(), decision=_decision(),
        )
    assert [r["turn_index"] for r in load_session(db_session, "s-x")] == [0, 1, 2]
    # 另一个会话从 0 重新开始
    record_turn(db_session, session_id="s-y", user_text="新会话", out=_out(), decision=_decision())
    assert load_session(db_session, "s-y")[0]["turn_index"] == 0


# ── 隐私 ────────────────────────────────────────────────────────────────

def test_pii_is_masked_before_persisting(log_enabled, db_session: Session):
    """手机号 / 邮箱在落库前必须已掩码——留档表是**长期**留存的数据，
    TTL 管不到它，泄漏面比会话存储大得多。"""
    secret_text = "我叫张三，电话 13800138000，邮箱 a.b@x.com，想买汉"
    record_turn(
        db_session, session_id="s-pii", user_text=secret_text,
        out=_out("已收到 13800138000"), decision=_decision(),
    )
    row = load_session(db_session, "s-pii")[0]
    assert "13800138000" not in row["user_text"]
    assert "a.b@x.com" not in row["user_text"]
    assert "13800138000" not in row["reply_text"]
    assert "13800138000" not in json_text(db_session, "s-pii")
    # 未脱敏的部分要原样保留，否则复现时输入就不是原话了
    assert "想买汉" in row["user_text"]


def json_text(db: Session, session_id: str) -> str:
    """把该会话落库的**所有**文本列拼起来——防止只检查了 user_text 漏掉别的列。"""
    rows = db.execute(
        select(AgentTurnLog).where(AgentTurnLog.session_id == session_id)
    ).scalars().all()
    return " ".join(f"{r.user_text} {r.reply_text}" for r in rows)


# ── 绝不阻断请求 ─────────────────────────────────────────────────────────

def test_write_failure_never_propagates(log_enabled, db_session: Session, monkeypatch):
    """留档失败只能记 warning。留档是观测手段，让它把用户请求带崩是本末倒置。"""

    class Boom:
        def __getattr__(self, _name):
            raise RuntimeError("db 炸了")

    monkeypatch.setattr(conversation_log, "AgentTurnLog", Boom)
    record_turn(
        db_session, session_id="s-boom", user_text="汉怎么样",
        out=_out(), decision=_decision(),
    )  # 不抛即通过


# ── 保留期 ──────────────────────────────────────────────────────────────

def test_purge_expired_removes_old_rows(log_enabled, db_session: Session, monkeypatch):
    monkeypatch.setenv("AGENT_CONVERSATION_LOG_RETENTION_DAYS", "7")
    get_settings.cache_clear()
    record_turn(db_session, session_id="s-old", user_text="旧", out=_out(), decision=_decision())
    record_turn(db_session, session_id="s-new", user_text="新", out=_out(), decision=_decision())
    stale = db_session.query(AgentTurnLog).filter(AgentTurnLog.session_id == "s-old").one()
    stale.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    db_session.commit()

    removed = purge_expired(db_session)
    assert removed == 1, "过期一行应被清掉"
    assert load_session(db_session, "s-old") == []
    assert len(load_session(db_session, "s-new")) == 1


def test_retention_zero_means_never_purge(log_enabled, db_session: Session, monkeypatch):
    """保留期 0 = 永不自动清理。这是给「法规要求长期留存」留的出口，
    关掉自动清理、交给外部流程接管——所以它必须是**明确的 0** 而不是默认值。"""
    monkeypatch.setenv("AGENT_CONVERSATION_LOG_RETENTION_DAYS", "0")
    get_settings.cache_clear()
    record_turn(db_session, session_id="s-keep", user_text="留", out=_out(), decision=_decision())
    stale = db_session.query(AgentTurnLog).one()
    stale.created_at = datetime.now(timezone.utc) - timedelta(days=999)
    db_session.commit()
    assert purge_expired(db_session) == 0
    assert len(load_session(db_session, "s-keep")) == 1


# ── 端到端接线 ──────────────────────────────────────────────────────────

def test_agent_turns_are_actually_logged(log_enabled, client, db_session: Session):
    """接线点必须真在链路上，而不只是「函数存在」。

    respond() 有 15 处 return，逐处包必然漏几个；因此接在 `handle()` 这个
    唯一对外出口。本条证明它确实在写——漏接是这类观测设施最常见的失败方式，
    而且漏了不会报错、测试也照样绿。
    """
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    src = make_source(db_session, name="汽车之家")
    byd = make_brand(db_session, name="比亚迪", source=src)
    han = make_series(db_session, byd, name="汉", body_type="sedan",
                      energy_types=("BEV",), source=src)
    year = make_year(db_session, han)
    make_variant(db_session, han, year, config_version="旗舰版", energy_type="BEV",
                 price_cny="220000", source=src,
                 facts=[("参数信息", "CLTC纯电续航里程(km)", "605", "km", "CLTC")])
    db_session.commit()

    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": "汉怎么样"})

    rows = load_session(db_session, sid)
    assert rows, "走了 agent 接口却没留下任何留档——接线没生效"
    assert rows[0]["user_text"] == "汉怎么样"
    assert rows[0]["intent"], "留档必须带 intent，否则排障时看不出走了哪条链路"
    assert han.id in (rows[0]["series_ids"] or []), (
        f"留档必须记下解析出的车系（这正是「选了哪台车」这类缺陷的复现关键），"
        f"实际 {rows[0]['series_ids']}"
    )


def test_early_return_branches_are_also_logged(
    log_enabled, client, db_session: Session
):
    """P2-1 回归（2026-10-06 独立审查）：闲聊与版本差异**也必须留档**。

    `respond()` 有两处 return 在 `decision_sink.append` **之前**（`is_chatty` 与
    `asks_variant_diff`），此前这两类轮次 `agent_turn_logs` **一行都不写**——
    而版本差异正是确定性链路、恰是「静默错答」高发区，最需要留档。

    `handle()` 的注释曾写着「此处是唯一对外出口，一处覆盖全部分支」，被本条实测推翻。
    """
    from tests.seed import (
        make_brand,
        make_series,
        make_source,
        make_variant,
        make_year,
    )

    src = make_source(db_session, name="汽车之家")
    brand = make_brand(db_session, name="问界", source=src)
    s = make_series(db_session, brand, name="问界M6", body_type="suv",
                    energy_types=("REEV",), source=src)
    year = make_year(db_session, s)
    make_variant(db_session, s, year, config_version="旗舰版", energy_type="BEV",
                 price_cny="230000", source=src,
                 facts=[("参数信息", "CLTC纯电续航里程(km)", "605", "km", "CLTC")])
    db_session.commit()

    cases = [
        ("你好", "early_return:is_chatty"),
        ("问界M6各版本有什么区别", "early_return:asks_variant_diff"),
    ]
    for msg, expect_rule in cases:
        sid = client.post("/api/v1/agent/sessions").json()["session_id"]
        client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": msg})
        rows = load_session(db_session, sid)
        assert rows, f"「{msg}」走了早期返回分支，却一行都没留档"
        rule = rows[0]["matched_rule"] or ""
        assert rule.startswith("early_return:"), (
            f"「{msg}」的留档应标明走了哪条早期分支，期望 {expect_rule}，实际 {rule!r}"
        )


def test_early_return_does_not_dispatch_shadow_route(
    log_enabled, client, db_session: Session, monkeypatch
):
    """哨兵决策**不得**触发 shadow 旁路派发。

    那两条分支原先 sink 为空 → 不派发；补了哨兵后若照旧派发，会改变既有 shadow 行为。
    """
    from app.agent import engine as engine_mod

    calls: list[tuple] = []
    monkeypatch.setattr(
        engine_mod.AgentEngine, "_spawn_shadow_route",
        lambda self, message, profile, decision: calls.append((message, decision)),
    )
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    client.post(
        f"/api/v1/agent/sessions/{sid}/messages", json={"message": "你好"}
    )
    assert not calls, "闲聊分支不应派发 shadow 旁路"


def test_pii_plate_and_split_id_are_masked(log_enabled, db_session: Session):
    """2026-10-06 独立审查实测补齐：**车牌**与**身份证分段**此前不脱敏。

    完整 18 位身份证会被「≥15 位连续数字」命中，但**拆开写**（地区码 / 生日 / 顺序码
    分几次打进对话）就漏。车牌则是汽车场景唯一无法用通用规则覆盖的强标识。
    """
    from app.agent.conversation_log import _mask_text

    raw = "我的车牌苏A12345，身份证 110101 和 19900101，手机 13800138000"
    masked = _mask_text(raw)
    assert "苏A12345" not in masked, f"车牌未脱敏：{masked}"
    assert "110101" not in masked, f"身份证地区码未脱敏：{masked}"
    assert "19900101" not in masked, f"身份证生日段未脱敏：{masked}"
    assert "13800138000" not in masked, f"手机号未脱敏：{masked}"
    # 未脱敏部分要保留，否则复现时输入就不是原话了
    assert "我的车牌" in masked


def test_plate_mask_does_not_eat_spec_text(log_enabled):
    """车牌正则不得误伤参数/型号文本（否则留档失去复现价值）。"""
    from app.agent.conversation_log import _mask_text

    for safe in ("汉L的续航605km", "问界M6指导价23.00万", "轴距2950mm", "凯美瑞2.0T"):
        assert _mask_text(safe) == safe, f"参数文本被车牌规则误伤：{safe!r} -> {_mask_text(safe)!r}"


def test_multi_turn_session_logs_every_turn(log_enabled, client, db_session: Session):
    """多轮上下文型缺陷（「第2轮突然跑偏」）只有留全每一轮才查得出来。"""
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    for msg in ("预算20万", "家用", "有什么热门的车"):
        client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": msg})
    rows = load_session(db_session, sid)
    assert len(rows) == 3, f"三轮对话应留 3 条，实际 {len(rows)}"
    assert [r["user_text"] for r in rows] == ["预算20万", "家用", "有什么热门的车"]
