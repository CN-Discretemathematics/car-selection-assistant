"""封闭失效观测：把「越界 / 类型失守 / 格式失守」与「引文编造」分开（2026-10-04）。

## 为什么要有这一组测试

切流判据（`soft_prefs` 模块 docstring）要求「越界率必须恒为 0」。但过去它在运行时
**根本测不出来**：

- `sanitize` → `_pick_value` 把词表外的值直接丢成 `None`——**静默丢弃**；
- `tools/eval_soft_prefs.py` 统计的 `illegal_values` 取的是 **sanitize 之后**的输出，
  所以**必然为 0**——那是构造性的，不是「模型没越界」的证据；
- `log_shadow` 落的 `prefs` 同样是 sanitize 后的。

于是判据形同虚设：本组测试钉住观测链路（`_pick_value` 收集 → `log_shadow` 落
`illegal`），让这条判据**有它需要的观测点**。

## 四种丢弃原因，只有三种算「封闭失效」

| 原因 | 含义 | 记为 illegal？ |
| --- | --- | --- |
| 值是字符串但不在封闭词表内 | 输出空间被 schema 封闭正在失效 | `out_of_enum` |
| 值不是字符串（如 household 给了 `3`） | 类型侧失守 | `bad_type` |
| 响应根本不是 JSON 对象 | 格式侧失守 | `bad_format` |
| 值合法但 evidence 不是用户原话子串 | 第二道防线**正常工作** | ❌ 不记 |

漏掉后两类，会让「越界率恒为 0」在模型狂吐错类型 / 非 JSON 时**假绿**。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.agent import soft_prefs as sp


def _item(value: object, evidence: str = "我平时通勤") -> dict:
    return {"value": value, "evidence": evidence}


def _fields(illegal: list[dict]) -> set[str]:
    return {e["field"] for e in illegal}


def _reasons(illegal: list[dict]) -> set[str]:
    return {e["reason"] for e in illegal}


# ── 越界 vs 引文编造：必须分得开 ────────────────────────────────────────────
def test_out_of_enum_value_is_recorded():
    """值是字符串但不在词表内 → 记 `out_of_enum`，且**不进**清洗结果。"""
    illegal: list[dict] = []
    out = sp.sanitize({"usage_scenario": _item("商务舱")}, "我平时通勤", illegal)

    assert out == {}, "越界值不得进入清洗结果"
    assert _fields(illegal) == {"usage_scenario"}
    assert _reasons(illegal) == {"out_of_enum"}
    assert illegal[0]["value"] == "商务舱"


def test_fabricated_evidence_is_not_illegal():
    """值合法但引文不是原话子串 → **不得**记为 illegal（防线二正常工作）。"""
    illegal: list[dict] = []
    out = sp.sanitize(
        {"usage_scenario": {"value": "通勤", "evidence": "我每周去三次健身房"}},
        "我平时通勤",
        illegal,
    )

    assert out == {}
    assert illegal == [], (
        f"引文编造被误记为封闭失效（{illegal}）——两种拦截含义完全不同，"
        "混在一起会让人以为 schema 封闭失效"
    )


def test_bad_type_is_recorded():
    """值不是字符串也算封闭失效——只看词表外字符串会让判据在错类型时假绿。"""
    illegal: list[dict] = []
    out = sp.sanitize({"household_size": _item(3)}, "我平时通勤", illegal)

    assert out == {}
    assert _reasons(illegal) == {"bad_type"}
    assert _fields(illegal) == {"household_size"}


def test_illegal_is_collected_across_all_four_fields():
    """四个字段都要能报出归属，否则看不出模型在哪个字段上失守。"""
    illegal: list[dict] = []
    payload = {
        "usage_scenario": _item("商务舱"),
        "household_size": _item("9~12人"),
        "pain_points": [_item("音响改装"), _item("安全配置")],
        "priority_order": [_item("颜值")],
    }

    out = sp.sanitize(payload, "我平时通勤", illegal)

    assert out == {"pain_points": ["安全配置"]}, out
    assert _fields(illegal) == {
        "usage_scenario", "household_size", "pain_points", "priority_order",
    }
    assert {e["value"] for e in illegal} == {"商务舱", "9~12人", "音响改装", "颜值"}


# ── 去重：重复值不得吃光配额、把别的字段遮蔽掉 ────────────────────────────────
def test_duplicate_values_do_not_exhaust_the_budget():
    """同一个值刷屏时，**别的字段**的越界必须仍然可见。

    subagent 审查实测出的真问题：没有去重时，`pain_points` 给 30 个重复值就能
    吃光全部配额，`priority_order` 的越界完全不可见——而提示词点名禁写的
    恰恰是 `priority_order`。
    """
    illegal: list[dict] = []
    payload = {
        "pain_points": [_item("SUV") for _ in range(30)],
        "priority_order": [_item("颜值")],
    }

    sp.sanitize(payload, "我平时通勤", illegal)

    assert "priority_order" in _fields(illegal), (
        f"重复值吃光配额，把 priority_order 的越界遮蔽了：{illegal}"
    )


# ── 封顶：钉绝对值，不拿常量自身做断言 ──────────────────────────────────────
def test_illegal_list_is_capped():
    """条数封顶。断言用**绝对值**，否则把常量写成 30 测试照样绿。"""
    illegal: list[dict] = []
    sp.sanitize(
        {"pain_points": [_item(f"越界维度{i}") for i in range(50)]},
        "我平时通勤",
        illegal,
    )

    assert len(illegal) == 6, f"越界条数未按 6 封顶：{len(illegal)}"


def test_illegal_item_is_truncated():
    """单条截断。断言用**绝对值**（24 字），否则常量写成 300 测试照样绿。"""
    illegal: list[dict] = []
    sp.sanitize({"usage_scenario": _item("越" * 300)}, "我平时通勤", illegal)

    assert len(illegal) == 1
    assert len(illegal[0]["value"]) == 24, f"越界值未按 24 字截断：{len(illegal[0]['value'])}"


# ── 向后兼容 ────────────────────────────────────────────────────────────────
def test_sanitize_without_out_param_is_unchanged():
    """不传 illegal_out 时清洗结果必须逐位相同——本改动的兼容底线。"""
    payload = {
        "usage_scenario": _item("通勤"),
        "pain_points": [_item("商务舱"), _item("续航")],
    }

    with_illegal: list[dict] = []
    a = sp.sanitize(payload, "我平时通勤", with_illegal)
    b = sp.sanitize(payload, "我平时通勤")

    assert a == b, "传与不传 out-param 的清洗结果必须一致"
    assert sp.sanitize({"usage_scenario": _item("商务舱")}, "我平时通勤") == {}


def test_forbidden_key_with_null_value_is_not_recorded():
    """`{"body_type": null}` **不得**记成 `forbidden_field`。

    subagent 审查抓出：提示词明写「不该抽的东西（**务必输出 null**）」——
    模型**照做**却因顶层循环没有 null 豁免而被记成「风险最高的失效」。
    那是方向反了：判据惩罚守法、奖励违规。

    真正要抓的是 `body_type: "SUV"`（模型凭空编造硬约束），见
    `test_forbidden_hard_constraint_keys_are_recorded`。
    """
    illegal: list[dict] = []
    out = sp.sanitize(
        {"body_type": None, "energy_type": None, "budget": None, "color": ""},
        "我平时通勤",
        illegal,
    )

    assert out == {}
    assert illegal == [], f"「照提示词输出 null」被误记为失效：{illegal}"


def test_unknown_fields_cannot_crowd_out_high_risk_reasons():
    """`unknown_field`（信息量最低）不得挤掉 `forbidden_field` / `out_of_enum`。

    subagent 审查实测：6 个无关键即可把配额占满，导致禁写硬约束键与越界值
    **一条都没记下**——最高风险的类别优先级最低，是一条实打实的假绿。
    """
    payload: dict = {f"k{i}": i for i in range(8)}
    payload["body_type"] = "SUV"
    payload["usage_scenario"] = {"value": "商务舱", "evidence": "我平时通勤"}

    illegal: list[dict] = []
    sp.sanitize(payload, "我平时通勤", illegal)

    reasons = {(e["field"], e["reason"]) for e in illegal}
    assert ("body_type", "forbidden_field") in reasons, illegal
    assert ("usage_scenario", "out_of_enum") in reasons, illegal
    assert len(illegal) <= 6, "封顶仍然生效"


def test_structurally_broken_items_are_recorded_as_bad_shape():
    """raw 不是 dict / 列表字段给了标量 → 记 `bad_shape`。

    **本用例的口径在 2026-10-04 翻转过**：原断言是「结构错误不算封闭失效」，
    理由是它没说「词表外的话」。现在改为**要记**，因为：

    - 「没按 schema 输出」本身就是封闭失效的一种；
    - 判据第 2 条要能回答「模型有没有乱吐字段」，而结构错误正是乱吐的一种；
    - 过去这类输入静默变成空列表，「`pain_points` 被写成字符串」**完全不可观测**。

    与「值是 `None`」的界线仍然清晰：`null` = 没答上来（不记），
    `["通勤"]` / `"续航"` = 答了但没按格式（记）。
    """
    illegal: list[dict] = []
    sp.sanitize({"usage_scenario": ["通勤"]}, "我平时通勤", illegal)

    assert [e["reason"] for e in illegal] == ["bad_shape"], illegal
    assert illegal[0]["field"] == "usage_scenario"


def test_list_field_given_a_scalar_is_bad_shape():
    """该给数组的字段给了标量 —— 同样记 `bad_shape`。"""
    illegal: list[dict] = []
    out = sp.sanitize({"pain_points": "续航"}, "我平时通勤", illegal)

    assert out == {}, "标量不得被当成单元素数组接受"
    assert [e["reason"] for e in illegal] == ["bad_shape"], illegal


def test_none_valued_field_is_still_not_recorded():
    """`null` = 没答上来 —— **不记**（与形状失守的界线）。

    实测踩坑：金标 25 条一度报出「40 条 bad_shape」，**全部是 None**。
    模型只会答一两个字段、其余给 null 是正常的；把它记成失效会让判据永远红灯。
    """
    illegal: list[dict] = []
    out = sp.sanitize(
        {
            "usage_scenario": None,
            "pain_points": None,
            "household_size": {"value": None, "evidence": "我平时通勤"},
            "priority_order": [{"value": None, "evidence": "我平时通勤"}],
        },
        "我平时通勤",
        illegal,
    )

    assert out == {}
    assert illegal == [], f"「没填」被误记为封闭失效：{illegal}"


def test_forbidden_hard_constraint_keys_are_recorded():
    """模型凭空吐出 `body_type` / `energy_type` / `budget` → 记 `forbidden_field`。

    这是本模块**风险最高**的一类失效：它们是 SQL 硬约束，直接下推 `WHERE`，
    模型猜错会**砍掉整个候选集**。而模块 docstring 判据第 2 条点名的正是这三个键
    ——此前它们完全不可观测。
    """
    illegal: list[dict] = []
    out = sp.sanitize(
        {
            "usage_scenario": _item("通勤"),
            "body_type": "SUV",
            "energy_type": "BEV",
            "budget": "20万",
        },
        "我平时通勤",
        illegal,
    )

    # 照常丢弃，绝不进画像
    assert out == {"usage_scenario": "通勤"}, out

    forbidden = [e for e in illegal if e["reason"] == "forbidden_field"]
    assert {e["field"] for e in forbidden} == {"body_type", "energy_type", "budget"}, illegal


def test_unknown_top_level_keys_are_recorded():
    """非禁写、也不在字段表里的键 → 记 `unknown_field`（与禁写键分开）。"""
    illegal: list[dict] = []
    sp.sanitize({"usage_scenario": _item("通勤"), "color": "红色"}, "我平时通勤", illegal)

    reasons = {e["field"]: e["reason"] for e in illegal}
    assert reasons == {"color": "unknown_field"}, illegal


# ── shadow 日志：判据需要的观测点必须真的落在日志里 ──────────────────────────
def test_shadow_log_carries_illegal_field(caplog):
    """`log_shadow` 的 JSON 里必须带 `illegal`，否则线上无从统计越界率。"""
    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        sp.log_shadow("我平时通勤", {"usage_scenario": "通勤"}, False,
                      [{"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}])

    data = json.loads(
        [r for r in caplog.records if sp.SHADOW_LOGGER in r.getMessage()][0].getMessage()
    )
    assert data["illegal"] == [
        {"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}
    ], data
    # 既有字段不能被挤掉
    assert data["prefs"] == {"usage_scenario": "通勤"}
    assert data["logger"] == sp.SHADOW_LOGGER
    assert data["version"] == sp.SOFT_PREF_VERSION


def test_shadow_log_illegal_defaults_to_empty(caplog):
    """不传 illegal 时落空数组（不是缺字段）——统计脚本不该处理两种形状。"""
    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        sp.log_shadow("我平时通勤", None, False)

    data = json.loads(
        [r for r in caplog.records if sp.SHADOW_LOGGER in r.getMessage()][0].getMessage()
    )
    assert data["illegal"] == [], data


def _fake_llm(content: str, raises: Exception | None = None):
    class _FakeLLM:
        available = True

        async def chat(self, msgs, tools=None, temperature=0.2, json_mode=False, thinking=None):
            if raises is not None:
                raise raises
            return {"choices": [{"message": {"content": content}}]}

    return _FakeLLM()


def _shadow_payload(caplog) -> dict:
    return json.loads(
        [r for r in caplog.records if sp.SHADOW_LOGGER in r.getMessage()][0].getMessage()
    )


# ── status：没有它越界率会假绿 ──────────────────────────────────────────────
def test_status_ok_when_fields_passed(monkeypatch, caplog):
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")
    content = json.dumps({"usage_scenario": {"value": "通勤", "evidence": "我平时通勤"}})

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm(content)))

    assert _shadow_payload(caplog)["status"] == "ok"


def test_status_all_rejected_when_nothing_passes(monkeypatch, caplog):
    """调通了、但字段全被拦 → `all_rejected`（**不是** no_response）。"""
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")
    content = json.dumps({"usage_scenario": {"value": "通勤", "evidence": "我去健身房"}})

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm(content)))

    payload = _shadow_payload(caplog)
    assert payload["status"] == "all_rejected", payload
    assert payload["prefs"] is None
    # 引文编造**不算**封闭失效，但仍要能看出「模型确实响应过」
    assert payload["illegal"] == []


def test_status_no_response_on_timeout(monkeypatch, caplog):
    """超时 → `no_response`。这是最关键的一条：

    若把它与「没越界」混为一谈，**一次大面积超时会伪装成越界率 0**。
    """
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(
            sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm("", raises=TimeoutError()))
        )

    payload = _shadow_payload(caplog)
    assert payload["status"] == "no_response", payload
    assert payload["prefs"] is None
    assert payload["illegal"] == []


def test_status_no_response_when_client_unavailable(monkeypatch, caplog):
    """LLM 客户端不可用 → `no_response`（模型压根没上场，不该算「干净」）。"""
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    class _Down:
        available = False

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤", llm=_Down()))

    assert _shadow_payload(caplog)["status"] == "no_response"


def test_status_all_rejected_on_non_json(monkeypatch, caplog):
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm("我看心情")))

    payload = _shadow_payload(caplog)
    assert payload["status"] == "all_rejected"
    assert _reasons(payload["illegal"]) == {"bad_format"}


def test_status_no_response_on_empty_message(monkeypatch, caplog):
    """空消息这条出口也必须埋点。

    subagent 审查实测：删掉这一处的 `_set_status`，19+50 例**全绿**——
    正是本 PR 要消灭的那类「漏埋点假绿灯」。
    """
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "   "))

    payload = _shadow_payload(caplog)
    assert payload["status"] == "no_response", payload
    assert payload["prefs"] is None


def test_status_no_response_on_empty_content(monkeypatch, caplog):
    """空 content 这条出口同样必须埋点（审查实测：删掉它测试全绿）。"""
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm("   ")))

    payload = _shadow_payload(caplog)
    assert payload["status"] == "no_response", payload
    assert payload["prefs"] is None


def test_status_unknown_when_recorded_twice(monkeypatch, caplog):
    """**重复**埋点也必须显形为 `unknown`，不能静默取第一条。

    将来若有人在 `ok` 之后又补一次打点，`status[0]` 会悄悄取首个——
    测试若不钉住「恰好一条」，这条路径就会变成新的假绿灯。

    ⚠️ 假实现必须把 status 写进**调用方传进来的那个 out 列表**：自己新建一个
    list 的话，`run_if_enabled` 拿到的仍是空列表，这条用例就退化成测
    「零埋点」，于是无论 `len(status) == 1` 的判断在不在它都绿——
    第一版正是这么写的，被反向验证 M10 抓了出来。
    """
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    async def _double(message, llm=None, timeout_ms=None, illegal_out=None, status_out=None):
        sp._set_status(status_out, "ok")
        sp._set_status(status_out, "all_rejected")
        assert len(status_out) == 2, "本用例的前提是 status_out 里有两条"
        return None

    monkeypatch.setattr(sp, "extract_soft_prefs", _double)

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤"))

    assert _shadow_payload(caplog)["status"] == "unknown"


def test_illegal_status_is_rejected_and_logged(caplog):
    """拼错的 status 不得静默流下去——记 error 并落 `unknown`。"""
    with caplog.at_level(logging.ERROR, logger="app.agent.soft_prefs"):
        out: list[str] = []
        sp._set_status(out, "no-reponse")  # 故意拼错

    assert out == ["unknown"], out
    assert "非法 status" in caplog.text


def test_status_is_unknown_when_not_recorded(monkeypatch, caplog):
    """`run_if_enabled` 的兜底必须是显式的 `unknown`，不能猜成 `no_response`。

    猜成 `no_response` 会把「这条路径忘了埋点」掩盖掉：既看不出漏埋点，
    测试也永远绿。`unknown` 让漏埋点显形，由下游按「不可判定」处理。
    """
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    async def _no_status(*a, **k):
        return {"pain_points": [{"value": "续航", "evidence": "我平时通勤"}]}

    monkeypatch.setattr(sp, "extract_soft_prefs", _no_status)

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        asyncio.run(sp.run_if_enabled(object(), "我平时通勤"))

    assert _shadow_payload(caplog)["status"] == "unknown"


def test_run_if_enabled_threads_illegal_to_log(monkeypatch, caplog):
    """端到端：封闭失效从 `extract_soft_prefs` 一路走到 shadow 日志的 `illegal`。

    这是本组的**主断言**——前面几条各自只证明一段管道，
    只有它能证明「判据要的观测点真的存在」。
    """
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")
    content = json.dumps({
        "usage_scenario": {"value": "商务舱", "evidence": "我平时通勤"},
        "pain_points": [{"value": "续航", "evidence": "我平时通勤"}],
    }, ensure_ascii=False)

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        applied = asyncio.run(sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm(content)))

    assert applied is False, "shadow 模式不得落画像"
    data = json.loads(
        [r for r in caplog.records if sp.SHADOW_LOGGER in r.getMessage()][0].getMessage()
    )
    assert data["illegal"] == [
        {"field": "usage_scenario", "reason": "out_of_enum", "value": "商务舱"}
    ], data
    assert data["prefs"] == {"pain_points": ["续航"]}, data
    assert data["mode_applied"] is False


def test_non_json_response_is_recorded_as_bad_format(monkeypatch, caplog):
    """响应不是 JSON 对象时，判据不得**假绿**——必须记一条 `bad_format`。"""
    monkeypatch.setattr(sp, "get_mode", lambda: "shadow")

    with caplog.at_level(logging.INFO, logger=sp.SHADOW_LOGGER):
        applied = asyncio.run(
            sp.run_if_enabled(object(), "我平时通勤", llm=_fake_llm("我看心情"))
        )

    assert applied is False
    data = json.loads(
        [r for r in caplog.records if sp.SHADOW_LOGGER in r.getMessage()][0].getMessage()
    )
    assert data["illegal"], "非 JSON 响应没有记入 illegal —— 越界率会假绿"
    assert _reasons(data["illegal"]) == {"bad_format"}
    assert data["illegal"][0]["field"] == "<response>"
    assert data["prefs"] is None
