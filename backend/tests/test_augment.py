"""augment_eval_questions 校验逻辑测试（评审 E1/E2 回归：纯函数，无网络）。"""
from __future__ import annotations

from tools.augment_eval_questions import _validate


def test_semantic_bucket_dropped_constraint_rejected():
    """评审 E1：semantic 桶约束只存在于原文——改写丢失「纯电」必须拒绝，
    否则变体会在 v2 约束指标中静默消失。"""
    q = {"bucket": "semantic", "text": "15万以内想要一台纯电的SUV通勤用", "expect": {"series_id": 1}}
    ok, reason = _validate(q["text"], "15万左右想买个SUV代步", q, True)
    assert not ok and reason == "constraint"
    ok2, _ = _validate(q["text"], "15万以内买个纯电SUV代步", q, True)
    assert ok2


def test_phev_short_label_accepted():
    """评审 E2：PHEV 完整措辞（能加油能充电的插混）几乎不出现在改写体中——
    短标签「插混」必须同样被接受，否则该家族被 100% 确定性拒绝。"""
    q = {"bucket": "recommend", "expect": {"energy_type": "PHEV"}, "anchors": {}}
    ok, reason = _validate("想要一台能加油能充电的插混车", "插混SUV推荐一下", q, True)
    assert ok, reason


def test_digit_drift_rejected():
    q = {"bucket": "recommend", "expect": {}, "anchors": {}}
    ok, reason = _validate("预算15万买SUV", "预算20万买SUV", q, True)
    assert not ok and reason == "digits"


def test_measure_word_numerals_ignored():
    """评审 R4#5：量词数字（一台）不构成约束；但 座/口/人 邻接的数字是真实约束——
    「家里5口人」→「家里五口人」等价（示例原生形态不误拒），「5口→3口」座位漂移拒绝。"""
    q = {"bucket": "recommend", "expect": {"passengers": 5}, "anchors": {}}
    ok, _ = _validate("家里5口人，预算15万买SUV", "家里五口人，预算15万以内买个SUV", q, True)
    assert ok, "口/人量词数字等价"
    ok2, reason2 = _validate("家里5口人，预算15万买SUV", "家里三口人，预算15万以内买个SUV", q, True)
    assert not ok2 and reason2 == "digits", "座位数漂移（5口→3口）必须拒绝"
    ok3, reason3 = _validate("家里5口人，预算15万买SUV", "家里5口人，预算18万以内买个SUV", q, True)
    assert not ok3 and reason3 == "digits"


def test_resolver_failure_rejected():
    """实体可解析校验：解析不到原锚定车系的改写体拒绝（真值不可恢复）。"""
    q = {"bucket": "parameter", "text": "腾势Z9GT续航多少", "anchors": {"series_id": 1}}
    ok, reason = _validate("腾势Z9GT续航多少", "那台车续航怎么样", q, False, ["腾势Z9GT"])
    assert not ok and reason == "resolver"
