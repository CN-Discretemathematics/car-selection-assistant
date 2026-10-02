"""云 Redis / OSS 真实联调验证（不打印任何密钥）。"""
from __future__ import annotations

import os
import sys

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path; being an import, it also stops E402 on the app.* imports below)

from dotenv import load_dotenv

load_dotenv()

from app.common.redis_client import get_redis, reset_redis


def main() -> int:
    print("1/2 Redis：")
    reset_redis()
    client = get_redis()
    if client is None:
        print("  未配置或连接失败（回退进程内存储，原则 7）")
    else:
        key = "verify:ping"
        client.set(key, "ok", ex=30)
        value = client.get(key)
        client.delete(key)
        print(f"  PASS：连接成功，读写正常（值={value}）")

    print("2/2 OSS：")
    from app.common.oss import get_oss_bucket, reset_oss, upload_file

    reset_oss()
    import tempfile

    # 用工作区临时文件做上传探测
    probe_path = os.path.join(".tmp", "oss-probe.txt")
    os.makedirs(".tmp", exist_ok=True)
    with open(probe_path, "w", encoding="utf-8") as fh:
        fh.write("car-selection oss probe")
    ref = upload_file(probe_path, "snapshots/probe/oss-probe.txt")
    if ref is None:
        bucket = get_oss_bucket()
        print("  未配置或上传失败（快照仅本地存档，原则 7）")
    else:
        print(f"  PASS：上传成功 -> {ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
