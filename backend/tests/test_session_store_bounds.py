"""进程内 SessionStore 的两道上界（2026-10-02）。

此前进程内实现**两个上界都没有**：

1. **会话数无上界** —— TTL 只能淘汰「超过 1 小时（默认 `agent_session_ttl_seconds`）
   未活跃」的会话，所以稳态内存 = 最近一小时的**全部**会话数，随流量线性增长。
   进程内实现正是「未配置 Redis 时」的降级路径，撞上高流量即无界增长。
2. **每次读操作 O(n)** —— `_prune()` 在 `exists` / `get_profile` / `append_message`
   等**每一个**调用上遍历全部会话，会话多时 CPU 与内存一起线性涨。

现在：超上限按**最久未活跃**淘汰（与 TTL 同一把尺子），且清理最多每 30s 一次。
节流不改变可观察行为——TTL 本来就是秒级容忍的语义。
"""
from __future__ import annotations

import time

from app.agent.session import SessionStore


def _mk(ttl: int = 3600) -> SessionStore:
    return SessionStore(ttl_seconds=ttl)


def test_cap_evicts_least_recently_used():
    """超出 MAX_SESSIONS 时淘汰最久未活跃的会话。"""
    store = _mk()
    store.MAX_SESSIONS = 5
    ids = []
    for _ in range(8):
        sid = store.create()
        ids.append(sid)
        time.sleep(0.001)  # 让 last_seen 严格递增
    # 强制一次清理（绕过 30s 节流）
    store._prune(force=True)
    assert len(store._data) == 5
    # 最旧的 3 个应被淘汰，最新的 5 个保留
    for sid in ids[:3]:
        assert sid not in store._data
    for sid in ids[3:]:
        assert sid in store._data


def test_touching_a_session_protects_it_from_lru_eviction():
    """刚访问过的会话不该被当成「最久未活跃」——这正是 LRU 的意义。"""
    store = _mk()
    store.MAX_SESSIONS = 3
    a, b, c = store.create(), store.create(), store.create()
    time.sleep(0.001)
    d = store.create()
    # 回头碰一下 a（最旧的）
    store._get(a)
    store._prune(force=True)
    assert a in store._data, "刚访问过的会话不应被淘汰"
    assert b not in store._data, "未再访问的更旧会话应先出局"


def test_prune_is_throttled_by_default():
    """默认路径受节流窗口保护——连续读不会每次都全表扫。"""
    store = _mk()
    store.create()
    first = store._last_pruned
    store._prune()
    assert store._last_pruned == first, "未到间隔不应再扫一次"
    store._last_pruned = 0.0
    store._prune()
    assert store._last_pruned > 0, "超期后应重新扫描"


def test_ttl_expiry_still_applies():
    """节流不能把 TTL 语义改掉：过期的仍要淘汰（force 路径）。"""
    store = _mk(ttl=0)
    sid = store.create()
    time.sleep(0.01)
    store._prune(force=True)
    assert sid not in store._data


def test_delete_forces_immediate_prune():
    """delete 要的是「现在就没了」，不能等节流窗口。

    注意构造顺序：超上限时淘汰的是**最久未活跃**的那个，所以要让被删的会话
    是**较新**的，否则它会在 delete 之前就已被 LRU 淘汰。
    """
    store = _mk()
    store.MAX_SESSIONS = 1
    drop, keep = store.create(), store.create()
    assert store.delete(keep) is True
    assert keep not in store._data
    assert drop not in store._data, "force prune 应同时把超限的旧会话淘汰掉"


def test_zero_ttl_is_honoured_not_treated_as_absent():
    """ttl_seconds=0 意为「立即过期」；原先 `or default` 会静默变成 3600s。"""
    store = SessionStore(ttl_seconds=0)
    assert store._ttl == 0
    sid = store.create()
    time.sleep(0.01)
    store._prune(force=True)
    assert sid not in store._data
