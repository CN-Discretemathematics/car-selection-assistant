"""会话存储（本地开发为进程内 + TTL；生产替换为 Redis，接口保持一致）。

接口刻意与 Redis 语义对齐（get/set/append + TTL），后续切换只需替换实现。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from app.common.config import get_settings

MAX_MESSAGES = 50  # 会话消息历史上限（超出后保留最近 50 条；生产 Redis 实现沿用）


@dataclass
class _Session:
    profile: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)  # [{role, content, at}]
    last_result: dict | None = None
    last_seen: float = 0.0


class SessionStore:
    # 2026-10-02：给进程内实现加两道**上界**，此前两者都没有：
    #
    # 1) MAX_SESSIONS —— TTL 只能淘汰「超过 1 小时未活跃」的会话，
    #    因此稳态内存 = 最近一小时的**全部**会话数，随流量线性增长、
    #    无上界。进程内实现用于未配置 Redis 的开发/降级场景，
    #    撞上高流量就是无界增长。超出上限按**最久未活跃**淘汰——
    #    与 TTL 同一把尺子，不引入第二套语义。
    # 2) PRUNE_INTERVAL_SECONDS —— 原先 _prune() 在**每一次**读操作上遍历
    #    全部会话，是 O(n)/请求；会话多时 CPU 开销与内存一起线性涨。
    #    改为最多每 30s 扫一次：TTL 本来就是秒级容忍的语义，
    #    30s 的清理延迟不改变可观察行为，却把摊销成本降到 O(1)/请求。
    MAX_SESSIONS = 2000
    PRUNE_INTERVAL_SECONDS = 30.0

    def __init__(self, ttl_seconds: int | None = None) -> None:
        # `or default` 会把 **0 当成未传**（0 是 falsy），使 ttl_seconds=0 静默变成
        # 3600s——「立即过期」在测试里根本设不出来，只能改用 -1 绕开。
        # 显式判 None 才符合参数语义。
        self._ttl = get_settings().agent_session_ttl_seconds if ttl_seconds is None else ttl_seconds
        self._data: dict[str, _Session] = {}
        self._last_pruned: float = 0.0

    def create(self) -> str:
        self._prune()
        session_id = uuid.uuid4().hex
        self._data[session_id] = _Session(last_seen=time.time())
        return session_id

    def _prune(self, *, force: bool = False) -> None:
        """按 TTL 淘汰过期会话；仍然超上限时再按最久未活跃淘汰。

        节流：默认最多每 PRUNE_INTERVAL_SECONDS 秒扫一次。
        `force=True` 供需要立即生效的路径（如 clear/delete）使用。
        """
        now = time.time()
        if not force and now - self._last_pruned < self.PRUNE_INTERVAL_SECONDS:
            return
        self._last_pruned = now

        expired = [sid for sid, s in self._data.items() if now - s.last_seen > self._ttl]
        for sid in expired:
            self._data.pop(sid, None)

        # TTL 之后仍可能超上限（最近一小时的会话数本身无上界）
        overflow = len(self._data) - self.MAX_SESSIONS
        if overflow > 0:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].last_seen)[:overflow]
            for sid, _s in oldest:
                self._data.pop(sid, None)

    def exists(self, session_id: str) -> bool:
        self._prune()
        return session_id in self._data

    def _get(self, session_id: str) -> _Session | None:
        self._prune()
        session = self._data.get(session_id)
        if session is not None:
            session.last_seen = time.time()
        return session

    def get_profile(self, session_id: str) -> dict:
        session = self._get(session_id)
        return dict(session.profile) if session else {}

    def set_profile(self, session_id: str, profile: dict) -> None:
        session = self._get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.profile = profile

    def append_message(self, session_id: str, role: str, content: str) -> None:
        session = self._get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.messages.append({"role": role, "content": content, "at": time.time()})
        if len(session.messages) > MAX_MESSAGES:
            session.messages = session.messages[-MAX_MESSAGES:]

    def history(self, session_id: str) -> list[dict]:
        session = self._get(session_id)
        return list(session.messages) if session else []

    def set_last_result(self, session_id: str, result: dict) -> None:
        session = self._get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.last_result = result

    def get_last_result(self, session_id: str) -> dict | None:
        session = self._get(session_id)
        return session.last_result if session else None

    def clear(self, session_id: str) -> bool:
        """清空会话记忆（画像 / 消息 / 上次结果），保留会话本身可用。

        为什么需要：画像在会话内是累积的，一旦某轮把约束理解错（例如把「不要奔驰」记成
        正向约束），后续每轮都会带着错误约束；用户需要能一键「重新开始对话」。
        """
        session = self._get(session_id)
        if session is None:
            return False
        session.profile = {}
        session.messages = []
        session.last_result = None
        return True

    def delete(self, session_id: str) -> bool:
        """彻底删除会话（其后任何请求都应视为会话不存在）。

        强制立即清理：调用方要的是「现在就没了」，不能等节流窗口。
        """
        self._prune(force=True)
        return self._data.pop(session_id, None) is not None


_session_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        from app.common.redis_client import get_redis

        redis_client = get_redis()
        if redis_client is not None:
            _session_store = RedisSessionStore(redis_client)
        else:
            _session_store = SessionStore()
    return _session_store


class RedisSessionStore:
    """Redis 会话存储（生产；多 worker 安全，接口与进程内 SessionStore 一致）。"""

    def __init__(self, client, ttl_seconds: int | None = None) -> None:
        self._client = client
        self._ttl = ttl_seconds or get_settings().agent_session_ttl_seconds

    def _profile_key(self, session_id: str) -> str:
        return f"agent:session:{session_id}:profile"

    def create(self) -> str:
        session_id = uuid.uuid4().hex
        self._client.set(self._profile_key(session_id), "{}", ex=self._ttl)
        return session_id

    def exists(self, session_id: str) -> bool:
        return bool(self._client.exists(self._profile_key(session_id)))

    def get_profile(self, session_id: str) -> dict:
        raw = self._client.get(self._profile_key(session_id))
        self._client.expire(self._profile_key(session_id), self._ttl)
        try:
            return json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return {}

    def set_profile(self, session_id: str, profile: dict) -> None:
        key = self._profile_key(session_id)
        self._client.set(key, json.dumps(profile, ensure_ascii=False), ex=self._ttl)

    def append_message(self, session_id: str, role: str, content: str) -> None:
        key = f"agent:session:{session_id}:messages"
        pipe = self._client.pipeline()
        pipe.rpush(key, json.dumps({"role": role, "content": content, "at": time.time()}, ensure_ascii=False))
        pipe.ltrim(key, -MAX_MESSAGES, -1)
        pipe.expire(key, self._ttl)
        pipe.execute()

    def history(self, session_id: str) -> list[dict]:
        key = f"agent:session:{session_id}:messages"
        raw_list = self._client.lrange(key, 0, -1)
        self._client.expire(key, self._ttl)
        out = []
        for raw in raw_list:
            try:
                out.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return out

    def set_last_result(self, session_id: str, result: dict) -> None:
        key = f"agent:session:{session_id}:last"
        self._client.set(key, json.dumps(result, ensure_ascii=False), ex=self._ttl)

    def get_last_result(self, session_id: str) -> dict | None:
        key = f"agent:session:{session_id}:last"
        raw = self._client.get(key)
        self._client.expire(key, self._ttl)
        try:
            return json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def clear(self, session_id: str) -> bool:
        """清空会话记忆（画像重置为空、删除消息与上次结果），保留会话可用。

        为什么需要：画像在会话内累积，某轮理解错（如把否定记成正向约束）会一直带下去，
        用户需要一键「重新开始对话」。实现上用一次 pipeline：删 messages/last、profile 置 {}，
        并统一续期，避免「删了一半」的中间态被读到。
        """
        profile_key = self._profile_key(session_id)
        if not self._client.exists(profile_key):
            return False
        pipe = self._client.pipeline()
        pipe.set(profile_key, "{}", ex=self._ttl)
        pipe.delete(f"agent:session:{session_id}:messages")
        pipe.delete(f"agent:session:{session_id}:last")
        pipe.execute()
        return True

    def delete(self, session_id: str) -> bool:
        """彻底删除会话（三个键一起删；其后请求按会话不存在处理）。"""
        profile_key = self._profile_key(session_id)
        if not self._client.exists(profile_key):
            return False
        self._client.delete(
            profile_key,
            f"agent:session:{session_id}:messages",
            f"agent:session:{session_id}:last",
        )
        return True
