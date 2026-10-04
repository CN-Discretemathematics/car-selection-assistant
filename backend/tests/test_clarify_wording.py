"""追问文案：回执已说过的侧重 + 一次性告知还差什么（2026-10-05 用户实测反馈）。

## 用户看到的原话

> 用户：我最看重动力，有哪些车推荐
> 助手：为了帮你挑到合适的车，先问一下：购车预算大概是多少？
> 用户：10~20万
> 助手：这辆车的主要用途是什么呢？

**「生硬」在哪**：用户明确说了「看重动力」，而两轮追问**一个字都没回应过**，
文案还是通用填充语（「为了帮你挑到合适的车，先问一下：」）。看起来就是
「说了没用」，于是像在被盘问。

## 本组测试守两件事

1. **必须回执**——已说过的侧重维度要出现在追问里（用 `_DIM_LABELS` 的既有措辞，
   不另造词）；
2. **不得重复**——待补清单里不能再列当前正在问的那一项。
"""
from __future__ import annotations

from app.agent.engine import next_clarification
from app.agent.schemas import Budget, UserProfile


def _profile(**kw) -> UserProfile:
    p = UserProfile()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_acknowledges_stated_priority():
    """用户说了看重动力，追问里必须出现「动力优先」。"""
    c = next_clarification(_profile(weights={"power": 0.5}))

    assert c is not None
    assert "动力" in c.question, c.question
    assert c.missing == ["budget"]


def test_multiple_priorities_are_listed():
    p = _profile(weights={"space": 0.5, "power": 0.3})
    c = next_clarification(p)

    assert "空间" in c.question and "动力" in c.question, c.question


def test_no_priority_stated_means_no_acknowledgement():
    """没说过任何侧重时**不得**凭空回执（「动力优先」是编造）。"""
    c = next_clarification(_profile())

    assert c is not None
    assert "优先" not in c.question, c.question
    assert c.question.startswith("预算大概多少"), c.question


def test_remaining_items_are_announced_once():
    """还差多项时一次性告知，避免「一轮一个」像盘问。"""
    c = next_clarification(_profile(weights={"power": 0.5}))

    assert "主要用途" in c.question, c.question
    assert "乘坐人数" in c.question, c.question
    assert "我一次排完" in c.question, c.question


def test_pending_list_excludes_the_question_being_asked():
    """主问题问预算，待补清单里**不得**再列预算（笨拙的重复）。"""
    c = next_clarification(_profile(weights={"power": 0.5}))

    tail = c.question.split("？", 1)[1]
    assert "预算" not in tail, f"待补清单重复了当前问题：{c.question}"


def test_single_remaining_item_gets_no_trailing_list():
    """只差一项时不再啰嗦地加括号说明。"""
    c = next_clarification(_profile(budget=Budget(min=1, max=2)))

    assert c is not None and c.missing == ["usage"]
    assert "我一次排完" not in c.question, c.question


def test_no_clarification_when_profile_is_complete():
    p = _profile(budget=Budget(min=1, max=2), usage=["通勤"], passengers=5)
    assert next_clarification(p) is None
