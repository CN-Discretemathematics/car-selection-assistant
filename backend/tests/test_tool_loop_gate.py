"""工具循环门禁的负向守护测试（第三轮审查 M3：门禁条件此前无任何测试报警）。

锁住两条「刚修好、极易再改坏」的语义：
1. 带预算/人数/用途的消息**不得**被工具循环接管（必须留在确定性推荐链）；
2. 易混短品牌名（大众/现代/银河/北京…）不得把日常用语误判成汽车语境。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agent.engine import mentions_known_brand
from tests.seed import make_brand, make_source


def _seed_brands(db: Session):
    source = make_source(db, name="汽车之家")
    for name in ("比亚迪", "大众", "银河", "丰田"):
        make_brand(db, name=name, source=source)
    db.commit()


def _in_loop(client: TestClient, message: str) -> bool:
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    body = client.post(f"/api/v1/agent/sessions/{sid}/messages", json={"message": message}).json()
    return bool((body.get("filters") or {}).get("tool_loop"))


def test_core_constraints_keep_message_in_recommendation_chain(client: TestClient, db_session: Session):
    """带核心约束（预算/人数/用途）的问法必须留在推荐链，不得被工具循环接管。"""
    _seed_brands(db_session)
    for message in (
        "20~30万有哪些增程SUV",
        "我平时3个人坐，有哪些增程SUV",
        "每天通勤用，解释一下有哪些新能源SUV",
    ):
        assert not _in_loop(client, message), f"不应被工具循环接管：{message}"


def test_ambiguous_short_brand_names_need_other_signal(db_session: Session):
    """易混短品牌名：日常用语不得算作汽车语境；有其它汽车信号时才成立。"""
    _seed_brands(db_session)
    # 日常用语（无其它汽车信号）→ 不算
    assert not mentions_known_brand(db_session, "帮我把这段话改得大众化一点")
    assert not mentions_known_brand(db_session, "现代人压力大怎么办")
    assert not mentions_known_brand(db_session, "银河系有多少颗恒星")
    assert not mentions_known_brand(db_session, "北京今天堵车吗")
    # 长品牌名（≥3 字）仍可直接成立
    assert mentions_known_brand(db_session, "解释一下比亚迪的销量表现怎么样")
    # 短名 + 其它汽车信号 → 成立
    assert mentions_known_brand(db_session, "大众朗逸这台车怎么样")
