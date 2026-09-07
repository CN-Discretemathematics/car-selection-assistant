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
    def __init__(self, ttl_seconds: int | None = None) -> None:
        self._ttl = ttl_seconds or get_settings().agent_session_ttl_seconds
        self._data: dict[str, _Session] = {}

    def create(self) -> str:
        session_id = uuid.uuid4().hex
        self._data[session_id] = _Session(last_seen=time.time())
        return session_id

    def _prune(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._data.items() if now - s.last_seen > self._ttl]
        for sid in expired:
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
