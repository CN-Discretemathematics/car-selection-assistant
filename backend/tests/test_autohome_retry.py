"""抓取链路的**重试策略**（2026-10-21，修 P4 缺陷台账 M 项）。

## 缺陷

- **库层零重试**：`fetch_series_index` / `fetch_sku_config` 一次失败就抛；
- **tools 层三处各写一个循环**：固定 `sleep(3.0)`、**无退避、无抖动**，
  且**什么都重试**（404、参数错、解析失败也重试 3 次）；
- 两层各自重试还会**叠加**（库层 3 次 × tools 层 3 次 = 9 次请求）。

## 守卫

- 瞬时错误（超时 / 5xx / 429 / 响应体截断）→ 指数退避 + **抖动**后重试；
- 确定性错误（404 / 业务返回码非 0）→ **立刻失败**，不浪费 3 次请求；
- 抖动让两次重试的等待时间**不相同**（否则多进程会整齐地打在对方同一刻）。
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from app.sources import autohome_sku


@pytest.fixture()
def sleeps(monkeypatch):
    """记录每次 sleep 的秒数（不真睡）。"""
    recorded: list[float] = []
    monkeypatch.setattr(autohome_sku.time, "sleep", recorded.append)
    return recorded


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "msg", None, None)  # type: ignore[arg-type]


# ── 分类：什么值得重试 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "err,transient",
    [
        (_http_error(503), True),
        (_http_error(429), True),
        (_http_error(500), True),
        (_http_error(404), False),
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (ValueError("业务返回码非 0"), False),
        (KeyError("解析缺字段"), False),
    ],
)
def test_transient_classification(err, transient):
    assert autohome_sku.is_transient_error(err) is transient


def test_truncated_body_counts_as_transient():
    """响应体被截断是**网络**症状（JSON 解析失败），值得重试。"""
    assert autohome_sku.is_transient_error(json.JSONDecodeError("x", "y", 0)) is True


# ── 退避与抖动 ──────────────────────────────────────────────────────────────


def test_transient_error_is_retried(sleeps):
    """瞬时错误会重试，并在**第 3 次**成功。"""
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("timed out")
        return "ok"

    assert autohome_sku.fetch_with_retry(_fn, label="t") == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2, "两次失败各等一次"


def test_backoff_grows(sleeps, monkeypatch):
    """第 n 次等待应比第 n-1 次**长**（指数退避，不是固定 3s）。

    把随机抖动**钉成 0**——否则断言是概率性的：去掉退避后两次等待都是 1.0±抖动，
    「后者更大」有 50% 概率仍然成立，变异测试 M2 实测就是这么漏掉的。
    """
    monkeypatch.setattr(autohome_sku.random, "uniform", lambda a, b: 0.0)

    def _fn():
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        autohome_sku.fetch_with_retry(_fn, attempts=3, base_delay=1.0)
    assert len(sleeps) == 2
    assert sleeps == [1.0, 2.0], f"退避未按 1s→2s 增长：{sleeps}"


def test_jitter_makes_waits_differ(sleeps):
    """等待时间必须**带抖动**——否则多进程会整齐地打在对方同一刻。

    ⚠️ 判据不能写成「两次等待不同」：有退避时它们本来就不同（1.0 与 2.0），
    那样去掉抖动照样通过（2026-10-21 变异测试 M1 实测踩到）。
    真正的判据是：**等待值不恰好等于纯退避值**，且抖动幅度有上界。
    """
    def _fn():
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        autohome_sku.fetch_with_retry(_fn, attempts=3, base_delay=1.0, max_delay=60.0)
    assert len(sleeps) == 2
    assert sleeps[0] > 1.0, f"第一次等待应带抖动，实际 {sleeps[0]}（=纯退避值）"
    assert sleeps[0] <= 1.0 * (1 + autohome_sku.RETRY_JITTER), (
        f"抖动幅度超界：{sleeps[0]}"
    )


def test_permanent_error_is_not_retried(sleeps):
    """确定性错误**立刻失败**——重试 3 次也是同样结果，只是更打扰对方。"""
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise _http_error(404)

    with pytest.raises(urllib.error.HTTPError):
        autohome_sku.fetch_with_retry(_fn, label="t")
    assert calls["n"] == 1, "404 不该重试"
    assert sleeps == [], "404 不该等待"


def test_attempt_limit_is_respected(sleeps):
    """一直失败时，重试次数有上限，错误照常抛出。"""
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        autohome_sku.fetch_with_retry(_fn, attempts=3, label="t")
    assert calls["n"] == 3
    assert len(sleeps) == 2


# ── 接线：库层的两个抓取函数真的用了策略 ─────────────────────────────────────


def test_fetch_sku_config_retries_the_network(monkeypatch, sleeps):
    """`fetch_sku_config` 自己兜住网络抖动（调用方不必各自写循环）。"""
    calls = {"n": 0}

    class _Resp:
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"returncode": 0, "result": {}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("timed out")
        return _Resp()

    monkeypatch.setattr(autohome_sku.urllib.request, "urlopen", _urlopen)
    autohome_sku.fetch_sku_config("8433")
    assert calls["n"] == 2, "第一次超时后应重试"
    assert len(sleeps) == 1


def test_fetch_sku_config_does_not_retry_a_business_error(monkeypatch, sleeps):
    """业务返回码非 0 是**确定性**错误——不该重试（重试也是同样结果）。"""
    calls = {"n": 0}

    class _Resp:
        headers = {"Content-Type": "application/json"}

        def read(self):
            return '{"returncode": 500, "message": "参数错误"}'.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(autohome_sku.urllib.request, "urlopen", _urlopen)
    with pytest.raises(ValueError):
        autohome_sku.fetch_sku_config("8433")
    assert calls["n"] == 1, "业务错误不该重试"
    assert sleeps == []
