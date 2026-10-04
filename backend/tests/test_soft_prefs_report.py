"""soft_prefs shadow 报告聚合的口径测试（2026-10-04）。

## 这组测试守的是什么

切流判据写的是「越界率必须恒为 0」。聚合器最大的风险不是算错，而是**算出一个
漂亮的 0 却什么都没测到**：

- 越界率的分母必须只算 LLM **真的响应过**的记录。混进 `no_response`
  （超时/异常/空内容/客户端不可用）的话，**一次大面积超时就显示成越界率 0**。
- `status=unknown`（未埋点或重复埋点）必须**判不可判定**，不能当干净算。
- 脏行（被截断的 JSON、同一条 logger 上的非 JSON 错误日志）不得被当成记录。

分母这种最容易搞错的地方，光靠人读代码不够，必须有用例钉住。
"""
from __future__ import annotations

import itertools
import json

from app.agent import soft_prefs_report as rpt

#: `_row()` 默认产出**唯一**的 message_head——聚合器按去重后的独立消息算样本量，
#: 共用同一个 head 会被判成重复记录。确实要测重复的用例请显式传 message_head。
_seq = itertools.count()


def _row(**kw) -> str:
    base = {
        "logger": "app.agent.soft_prefs.shadow",
        "version": "soft-pref-v1",
        "mode_applied": False,
        "status": "ok",
        "illegal": [],
        "prefs": {"pain_points": ["续航"]},
        "message_head": f"我平时通勤-{next(_seq)}",
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def _prefixed(payload: str) -> str:
    """模拟生产日志的 formatter 前缀（含 docker 容器前缀的情形）。"""
    return f"2026-10-04 21:00:00,000 INFO app.agent.soft_prefs.shadow {payload}"


# ── 分母：只算 LLM 响应过的 ─────────────────────────────────────────────────
def test_denominator_excludes_no_response():
    """大量超时不进分母——否则越界率会被压成 0（假绿的经典形态）。"""
    rows = [_row(status="no_response", prefs=None) for _ in range(9)]
    rows.append(_row())

    report = rpt.aggregate(rows)

    assert report["responded"] == 1, report
    assert report["no_response"] == 9
    assert report["illegal_rate"] == 0.0
    # 关键：不得因为 9 条超时就把分母算成 10
    assert report["illegal_rate"] != 0.1


def test_illegal_rate_counts_only_responded():
    rows = [
        _row(illegal=[{"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}]),
        _row(),
        _row(status="no_response", prefs=None),
    ]

    report = rpt.aggregate(rows)

    assert report["responded"] == 2
    assert report["responded_with_illegal"] == 1
    assert report["illegal_rate"] == 0.5


def test_all_rejected_counts_into_denominator():
    """模型响应过了就算数——它「说错了」或「全被拦」都是有效观测。"""
    report = rpt.aggregate([_row(status="all_rejected", prefs=None)])

    assert report["responded"] == 1
    assert report["empty_preference_rate"] == 1.0


# ── unknown：不可判定，不能当干净 ───────────────────────────────────────────
def test_unknown_excluded_from_denominator_and_flagged():
    rows = [_row(status="unknown", prefs=None), _row()]

    report = rpt.aggregate(rows)

    assert report["responded"] == 1
    assert report["unknown"] == 1
    assert "不可判定" in report["verdict"]
    assert report["sample_ok"] is False


def test_dirty_status_is_treated_as_unknown_not_ok():
    """拼错的 status 既不进分母、也不被当成 ok。"""
    report = rpt.aggregate([_row(status="no-reponse", prefs=None)])

    assert report["responded"] == 0
    assert report["unknown"] == 1


# ── 脏行容错：坏行不得被当成记录 ────────────────────────────────────────────
def test_truncated_and_unrelated_lines_are_skipped():
    lines = [
        _prefixed(_row()),
        "这一行是截断的坏 JSON，不该被算成记录",
        "2026-10-04 21:00:03,000 INFO app.agent.soft_prefs LLM 调用失败（回退正则）：TimeoutError: ",
        "",
    ]

    report = rpt.aggregate(lines)

    assert report["total"] == 1, report
    assert report["responded"] == 1
    # 脏行必须**计数**：否则「日志被截断」和「日志本来就是空的」长得一模一样。
    # 空行不算脏行——它是正常的分隔，不该混进「被丢弃」里。
    assert report["skipped"] == 2, report


def test_brace_fallback_recovers_record_behind_garbage():
    """前面有垃圾、但含 logger 名 + 完整 JSON 的行，必须被括号兜底救回来。

    subagent 审查指出：原先那条「截断行」用例里没有 logger 名也没有大括号，
    只走到 `json.loads` 失败就结束了，**从未进入括号兜底分支**。
    """
    payload = _row()
    lines = [f"docker 前缀乱码 {payload} 尾部还有东西"]

    report = rpt.aggregate(lines)

    assert report["total"] == 1, report
    assert report["responded"] == 1


# ── 观测字段不可用：「没测」不等于「没越界」 ────────────────────────────────
def test_missing_illegal_key_is_not_treated_as_clean():
    """`illegal` 键**不存在**（旧记录）不得按「测了没越界」算。

    这是本工具最要紧的一条：`illegal` 字段 2026-10-04 才上线，
    上线前的记录一律没有这个键。若当空列表处理，它们会以干净身份进分母，
    给出「通过」——与本模块 docstring 骂 `eval_soft_prefs.py` 的
    「构造性的 0」是同一种病。
    """
    rows = []
    for _ in range(25):
        base = {
            "logger": "app.agent.soft_prefs.shadow",
            "version": "soft-pref-v1",
            "status": "ok",
            "prefs": {"pain_points": ["续航"]},
            "mode_applied": False,
            "message_head": "x",
        }
        rows.append(json.dumps(base, ensure_ascii=False))

    report = rpt.aggregate(rows)

    assert report["no_illegal_field"] == 25, report
    assert report["responded"] == 0, "缺观测字段的记录不得进分母"
    assert report["sample_ok"] is False
    assert "没有 illegal 字段" in report["verdict"]


def test_malformed_illegal_is_not_treated_as_clean():
    for bad in ("out_of_enum", {"a": 1}, ["out_of_enum"], [1, 2]):
        rows = [
            json.dumps({
                "logger": "app.agent.soft_prefs.shadow", "version": "soft-pref-v1",
                "status": "ok", "illegal": bad, "prefs": {"pain_points": ["续航"]},
                "mode_applied": False, "message_head": f"x{i}",
            }, ensure_ascii=False)
            for i in range(25)
        ]
        report = rpt.aggregate(rows)
        assert report["illegal_shape_errors"] == 25, (bad, report)
        assert report["responded"] == 0, (bad, report)
        assert report["sample_ok"] is False, bad


# ── 另外三条会给出「通过」的路径 ────────────────────────────────────────────
def test_all_rejected_blizzard_is_not_a_pass():
    """25 条全 `all_rejected` + `illegal=[]` → 越界率 0，但模型只在说废话。"""
    rows = [
        _row(status="all_rejected", prefs=None, message_head=f"m{i}") for i in range(25)
    ]

    report = rpt.aggregate(rows)

    assert report["illegal_rate"] == 0.0
    assert report["empty_preference_rate"] == 1.0
    assert report["sample_ok"] is False
    assert "空偏好率" in report["verdict"]


def test_mode_applied_nonzero_blocks_pass():
    """shadow 模式下 mode_applied 必须恒为 0；非 0 说明模式被搞错了。"""
    rows = [_row(mode_applied=True, message_head=f"m{i}") for i in range(25)]

    report = rpt.aggregate(rows)

    assert report["applied"] == 25
    assert report["sample_ok"] is False
    assert "mode_applied" in report["verdict"]


def test_duplicate_messages_do_not_inflate_the_sample():
    """10 句 × 3 副本不得凑成「30 条样本」——分母按去重后的独立消息算。"""
    rows = [_row(message_head=f"m{i}") for i in range(10) for _ in range(3)]

    report = rpt.aggregate(rows)

    assert report["total"] == 30
    assert report["responded"] == 10, report
    assert report["duplicates"] == 20, report
    assert report["sample_ok"] is False, "10 条独立样本不该够 20 的下限"


def test_exit_codes_distinguish_pass_fail_and_unknown():
    """"不可判定"必须单独一档，CI 才不会把「没测到」读成「通过」。"""
    clean = [_row(message_head=f"m{i}") for i in range(25)]
    dirty = [
        _row(illegal=[{"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}],
             message_head=f"d{i}")
        for i in range(25)
    ]
    untestable = [_row(status="unknown", prefs=None, message_head=f"u{i}") for i in range(25)]

    assert rpt.aggregate(clean)["exit_code"] == 0
    assert rpt.aggregate(dirty)["exit_code"] == 1
    assert rpt.aggregate(untestable)["exit_code"] == 2, "不可判定不得返回 0"


def test_main_returns_verdict_exit_code(tmp_path, monkeypatch, capsys):
    clean = tmp_path / "clean.log"
    clean.write_text(
        "\n".join(_row(message_head=f"m{i}") for i in range(25)), encoding="utf-8"
    )

    assert rpt.main(["--file", str(clean)]) == 0
    assert "通过" in capsys.readouterr().out


def test_router_shadow_lines_are_not_mixed_in():
    """`app.agent.router.shadow` 的记录不得混进来（两个 logger 前缀不同）。"""
    router_row = json.dumps({
        "logger": "app.agent.router.shadow",
        "version": "router-v1",
        "utterance": "20万预算",
        "llm_intent": "recommendation",
    })

    report = rpt.aggregate([_prefixed(_row()), router_row])

    assert report["total"] == 1
    assert report["version_distribution"] == {"soft-pref-v1": 1}


# ── 跨版本切分 ─────────────────────────────────────────────────────────────
def test_version_distribution_splits_records():
    rows = [_row(version="soft-pref-v1"), _row(version="soft-pref-v2")]

    report = rpt.aggregate(rows)

    assert report["version_distribution"] == {"soft-pref-v1": 1, "soft-pref-v2": 1}


# ── 样本强度：越界率 0 不等于「测过了」 ─────────────────────────────────────
def test_small_sample_cannot_be_verdict_pass():
    """5 条响应样本即使全干净，也不足以支撑切流判据。"""
    report = rpt.aggregate([_row() for _ in range(5)])

    assert report["illegal_rate"] == 0.0
    assert report["sample_ok"] is False
    # 「样本不足」是**判不了**，不是「不通过」——它意味着再攒数据，而不是路走不通
    assert report["verdict"].startswith("不可判定")
    assert "样本不足" in report["verdict"]
    assert report["exit_code"] == 2


def test_large_clean_sample_passes():
    rows = [_row() for _ in range(25)]
    report = rpt.aggregate(rows)

    assert report["sample_ok"] is True
    assert report["verdict"].startswith("通过")
    assert report["unknown"] == 0


# ── shadow 模式不得落画像 ───────────────────────────────────────────────────
def test_applied_counted_for_mode_misconfiguration():
    report = rpt.aggregate([_row(mode_applied=True)])

    assert report["applied"] == 1
    assert "必须恒为 0" in rpt.render_markdown(report)


# ── 报告渲染 ────────────────────────────────────────────────────────────────
def test_markdown_mentions_denominator_rule_and_verdict():
    report = rpt.aggregate([
        _row(illegal=[{"field": "pain_points", "reason": "bad_type", "value": "3"}]),
        _row(),
    ])

    md = rpt.render_markdown(report)

    assert "不通过" in md
    assert "不进分母" in md
    assert "bad_type" in md
    assert "pain_points" in md


def test_self_test_passes():
    assert rpt.main(["--self-test"]) == 0
