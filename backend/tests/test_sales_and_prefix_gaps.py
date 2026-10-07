"""上线前审查抓到的三处修复：品牌对比被抢（F1）、前缀词表顺序（F5）、兄弟选取非确定性（F6）。

F1 是**本批引入的回归**：扩销量榜词表后，「大众和丰田哪个**车**销量最好」被抢成全库榜，
回一份带月份、带「39,651 辆」、带引用的**全库前十**，完全不提大众和丰田。
`0.7a2` 那个分支调 `sales_ranking(db, limit=10)`，**profile 压根没传进去**，所以
价格/年份/车身限定词能被 `_SALES_RANKING_QUALIFIER_RE` 挡住，**唯独品牌挡不住**。

门禁测试的重写（同批第 4 项）在 `tests/test_answer_scope_and_gates.py`。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.agent.routing import (
    _brand_count_in_message,
    decide_route,
    llm_intent_executable,
)
from app.agent.schemas import UserProfile
from app.catalog.brands import _strip_leading_prompt, brand_candidates_in_message
from app.catalog.series_index import resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year


def _seed(db: Session) -> None:
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    make_brand(db, name="丰田", source=src)
    make_brand(db, name="零跑", source=src)
    for name in ("朗逸", "卡罗拉"):
        s = make_series(db, vw, name=name, source=src, positioning="紧凑型车")
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


# ── F1 两品牌对比不该被抢成全库榜 ───────────────────────────────────────────


def test_two_brand_comparison_is_not_a_catalog_ranking(db_session: Session) -> None:
    """「大众和丰田哪个车销量最好」是**两品牌对比**，不是要全库榜单。"""
    _seed(db_session)
    for q in ("大众和丰田哪个车销量最好", "大众和丰田哪个品牌销量最好",
              "大众和丰田哪个车卖得最好", "大众和丰田哪个车销量最好一些"):
        assert _brand_count_in_message(db_session, q) >= 2, f"前提：{q} 里有 ≥2 个品牌"
        decision = decide_route(q, {}, False, UserProfile(), [], db_session)
        assert decision.intent != "sales_ranking", (
            f"「{q}」被抢成全库榜了（matched_rule={decision.matched_rule}）——"
            f"用户点的是大众和丰田，回答里却只有全库前十"
        )


def test_real_catalog_ranking_questions_still_answered(db_session: Session) -> None:
    """收窄不能误伤**真正**的全库榜问句。"""
    _seed(db_session)
    for q in ("什么车销量最好", "什么车卖得最好", "什么车最好卖",
              "哪个品牌销量第一", "本月销量榜", "热门车有哪些"):
        assert _brand_count_in_message(db_session, q) < 2, f"前提：{q} 不是多品牌对比"
        decision = decide_route(q, {}, False, UserProfile(), [], db_session)
        assert decision.intent == "sales_ranking", f"「{q}」应仍是全库榜单问句"


def test_vehicle_pair_comparison_still_blocked_by_resolved(db_session: Session) -> None:
    """车系对比靠 `not resolved` 挡，与本轮无关，但要确认没被破坏。"""
    _seed(db_session)
    resolved = list(resolve_series(db_session, "朗逸和卡罗拉哪个车销量最好"))
    decision = decide_route("朗逸和卡罗拉哪个车销量最好", {}, False, UserProfile(),
                            resolved, db_session)
    assert decision.intent != "sales_ranking"


def test_llm_override_also_blocked(db_session: Session) -> None:
    """LLM 把品牌对比判成 sales_ranking 时，`llm_intent_executable` 必须同样挡掉。

    否则它绕过正则路由直接改了执行分支——那正是 `arbitrate_route` 注释里警告的情形。
    """
    _seed(db_session)
    for q in ("大众和丰田哪个车销量最好", "大众和丰田哪个车卖得最好"):
        assert not llm_intent_executable("sales_ranking", q, {}, False, UserProfile(),
                                         [], db_session), (
            f"LLM 路径也必须挡下「{q}」"
        )


# ── F5 前缀词表顺序 ─────────────────────────────────────────────────────────


def test_longer_question_prefix_wins(db_session: Session) -> None:
    """「我想买」排在「想买辆」前面时，「我想买辆大众」会先被剥成「辆大众」。

    上线前审查实测（修复前）：
        「我想买大众」   → '大众'  ✓
        「想买大众」     → '大众'  ✓
        「我想买辆大众」 → '辆大众' ✗   ← 同一句话，少个「我」就对了
        「我打算买辆大众」→ '辆大众' ✗

    直接钉 `_strip_leading_prompt` 这个纯函数（不绕数据库），它就是出问题的那个环节。
    """
    _seed(db_session)
    for raw, expect in (
        ("我想买大众", "大众"),
        ("想买大众", "大众"),
        ("我想买辆大众", "大众"),
        ("我打算买辆大众", "大众"),
        ("帮我买大众", "大众"),
        ("请问大众", "大众"),
        ("那大众", "大众"),
        ("大众", "大众"),
        ("零跑", "零跑"),
    ):
        assert _strip_leading_prompt(raw) == expect, (
            f"_strip_leading_prompt({raw!r}) 应得 {expect!r}"
        )


def test_budget_prefix_is_stripped(db_session: Session) -> None:
    """「预算20万大众」——前缀带数字，用正则剥，不在词表里。"""
    _seed(db_session)
    assert _strip_leading_prompt("预算20万大众") == "大众"
    assert _strip_leading_prompt("预算20万大众和零跑哪个好") != "大众"


def test_longer_prefix_end_to_end(db_session: Session) -> None:
    """端到端：前缀写法要真的报出品牌。"""
    _seed(db_session)
    for q in ("我想买大众和零跑哪个好", "我想买辆大众和零跑哪个好"):
        got = sorted(brand_candidates_in_message(db_session, q))
        assert "大众" in got, f"「{q}」应报出大众。实得={got}"


# ── F6 兄弟品牌行选取必须确定性 ─────────────────────────────────────────────

# ⚠️ 这条测试**重写过两次**。第一版造的是「星原空行」去借「星原」——兄弟**只有一个**，
#    `key=len` 与 `key=(-len, s)` 的结果必然相同，**它根本不可能变红**（变异实测：
#    把 `(-len(s), s)` 改回 `len/reverse=True`，这条测试照样绿）。这跟第六轮审查
#    抓到的那条「空转不变量测试」是同一类缺陷。
#
#    第二版才造出真正的**等长平局**：「长安启源」的兄弟是「长安」（前缀命中）与
#    「启源」（后缀命中），**都是 2 字**——这才是 `key=len` 排不动的场景。
#
#    为什么必须跨 `PYTHONHASHSEED` 子进程断言：旧排序靠 `sorted` 的**稳定性**，
#    等长时保的是 `set` 迭代顺序，即哈希随机化。单进程跑等于掷一次硬币
#    （实测旧代码 seed 0/1/2/3/7/11/13 → 长安，seed 17 → 启源）。

# 探针代码：单独进程里建库并调用 `_brand_active_series`，打印 JSON。
# 用 `-c` 传源码（不经 shell，无引号问题），免得依赖 `.tmp/` 这类被 gitignore 的文件。
_SEED_PROBE = r"""
import json, os, sys
os.environ.update({
    "DATABASE_URL": "sqlite://", "AUTO_CREATE_TABLES": "false",
    "DEV_ECHO_CODES": "true", "ADMIN_API_TOKEN": "t", "DEEPSEEK_API_KEY": "",
    "RETRIEVAL_BACKEND": "inmemory", "REDIS_URL": "", "OSS_ENDPOINT": "",
    "OSS_BUCKET": "", "OSS_ACCESS_KEY_ID": "", "OSS_ACCESS_KEY_SECRET": "",
    "SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_FROM": "",
    "AGENT_CONVERSATION_LOG_ENABLED": "false", "AGENT_ROUTER_MODE": "off",
})
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.agent.series_qa import _brand_active_series
from app.common import models  # noqa: F401
from app.common.database import Base
from tests.seed import make_brand, make_series, make_source

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
src = make_source(db, name="汽车之家")
qy = make_brand(db, name="启源", source=src)
ca = make_brand(db, name="长安", source=src)
make_brand(db, name="长安启源", source=src)   # 本体 0 款车，才会走到借兄弟这条路
make_series(db, qy, name="启源A07", source=src, positioning="紧凑型SUV")
make_series(db, ca, name="长安CS75", source=src, positioning="紧凑型SUV")
db.commit()
print("RESULT:" + json.dumps(_brand_active_series(db, "长安启源"), ensure_ascii=False))
"""

#: 等长平局按**码位升序**（「启」U+542F < 「长」U+957F）必须选「启源」。
#: 这是 `key=(-len(s), s)` 的直接后果，与哈希种子无关。
_EXPECTED = ["启源A07"]

#: 旧排序实测会分裂的种子区间（0/1/2/3/7/11/13 → 长安，17 → 启源）。
#: 取 5 个：旧代码要「全过」得 5 次都撞上同一侧，概率 ~1/32。
_SEEDS = ("0", "1", "2", "7", "17")


def test_sibling_borrow_is_deterministic_across_hash_seeds() -> None:
    """同长度的兄弟候选顺序**不能**取决于进程哈希随机化。

    原先 `sorted(..., key=len, reverse=True)` 只按长度排；`sorted` 稳定，等长时
    保的是 `set` 迭代顺序 → 答案随 `PYTHONHASHSEED` 变：同一句「长安启源有哪几款车」，
    线上可能答「启源A07」也可能答「长安CS75」，而款数恰好相同，肉眼极难发现。
    """
    backend = Path(__file__).resolve().parents[1]
    seen: dict[str, str] = {}

    for seed in _SEEDS:
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", _SEED_PROBE],
            cwd=backend, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert proc.returncode == 0, f"seed={seed} 探针崩溃：\n{proc.stderr[-800:]}"
        line = next(
            (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")),
            None,
        )
        assert line is not None, f"seed={seed} 探针没输出：\n{proc.stdout[-500:]}"
        got = json.loads(line[len("RESULT:"):])
        seen[seed] = json.dumps(got, ensure_ascii=False)
        assert got == _EXPECTED, (
            f"seed={seed} 应借「启源」的车系（等长平局按码位升序）。实得={got}"
        )

    assert len(set(seen.values())) == 1, f"答案随哈希种子变了：{seen}"
