"""OSS 对象存储客户端（网页快照/PDF/图片）。

- OSS_* 未配置或连接失败时 available=False，调用方只做本地存档（原则 7）；
- 连接失败不永久回退：每 60 秒重试一次（评审 M7）；
- 凭据从 Settings（.env / KMS）读取，绝不写代码。
"""
from __future__ import annotations

import logging
import time

import oss2

from app.common.config import get_settings

logger = logging.getLogger(__name__)

_client: oss2.Bucket | None = None
_tried: bool = False
_last_attempt: float = 0.0
RETRY_INTERVAL_SECONDS = 60.0


def get_oss_bucket() -> oss2.Bucket | None:
    """OSS Bucket 客户端（连接失败每 60 秒重试；健康客户端直接复用，评审 M-R5）。"""
    global _client, _tried, _last_attempt
    if _client is not None:
        return _client
    now = time.time()
    if _tried and now - _last_attempt < RETRY_INTERVAL_SECONDS:
        return None
    _tried = True
    _last_attempt = now
    settings = get_settings()
    if not (settings.oss_endpoint and settings.oss_bucket and settings.oss_access_key_id):
        return None
    try:
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        _client = oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket)
        _client.get_bucket_info()  # 连通性探测
    except Exception:  # noqa: BLE001 - OSS 不可用时回退本地存档
        _client = None
    return _client


def reset_oss() -> None:
    """测试辅助：重置连接缓存。"""
    global _client, _tried, _last_attempt
    _client = None
    _tried = False
    _last_attempt = 0.0


def upload_file(local_path: str, object_key: str) -> str | None:
    """上传本地文件到 OSS；成功返回对象键（oss://bucket/key），失败返回 None（必须留痕，评审 M-R6）。"""
    bucket = get_oss_bucket()
    if bucket is None:
        return None
    try:
        bucket.put_object_from_file(object_key, local_path)
        settings = get_settings()
        return f"oss://{settings.oss_bucket}/{object_key}"
    except Exception:  # noqa: BLE001 - 上传失败回退本地存档，但不能静默
        logger.exception("OSS 上传失败（回退本地存档）：%s", object_key)
        return None


def snapshot_object_key(relative_path: str) -> str:
    """快照/图片的统一对象键前缀（中和 ".." 防前缀逃逸，评审 L-R7）。"""
    cleaned = relative_path.lstrip("/").replace("..", "_")
    return f"snapshots/{cleaned}"
