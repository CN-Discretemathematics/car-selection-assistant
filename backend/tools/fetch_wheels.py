"""依赖引导脚本（沙箱环境专用）。

背景：本环境沙箱拦截 python 向 tempfile.mkdtemp 目录写文件，导致 pip 无法解包；
因此这里不走 pip：直接从清华 PyPI 镜像下载 wheel 并解包到 vendor/，
配合 PYTHONPATH=vendor;. 使用（python -m pytest / alembic / uvicorn）。

在普通（非沙箱）环境中，仍可直接使用：
    python -m pip install --target vendor -r requirements.txt

依赖版本清单来自 requirements.txt 在 2026-08 的完整解析结果，
升级依赖时请同步更新 PACKAGES。
"""
from __future__ import annotations

import os
import re
import ssl
import sys
import time
import urllib.request
import zipfile
from urllib.parse import urljoin

BASE = "https://pypi.tuna.tsinghua.edu.cn/simple/"
WHEELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".wheels")
TARGET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")

_CTX = ssl.create_default_context()
_UA = {"User-Agent": "pip/26.1.2 (car-selection bootstrap)"}

PACKAGES: dict[str, str] = {
    "fastapi": "0.141.1",
    "uvicorn": "0.52.4",
    "sqlalchemy": "2.0.52",
    "alembic": "1.19.1",
    "pydantic": "2.13.4",
    "pydantic-core": "2.46.4",
    "pydantic-settings": "2.15.0",
    "pytest": "9.1.1",
    "httpx": "0.28.1",
    "starlette": "1.6.0",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.4",
    "annotated-doc": "0.0.5",
    "annotated-types": "0.8.0",
    "anyio": "4.14.2",
    "certifi": "2026.7.22",
    "click": "8.5.0",
    "colorama": "0.4.6",
    "greenlet": "3.5.5",
    "h11": "0.16.0",
    "httpcore": "1.0.9",
    "httptools": "0.8.0",
    "idna": "3.19",
    "iniconfig": "2.3.0",
    "mako": "1.4.1",
    "markupsafe": "3.0.3",
    "packaging": "26.3",
    "pluggy": "1.6.0",
    "pygments": "2.21.0",
    "python-dotenv": "1.2.3",
    "pyyaml": "6.0.3",
    "watchfiles": "1.2.0",
    "websockets": "17.1",
    # 云端中间件接入（latest = 取镜像最新稳定版）
    "psycopg": "latest",               # psycopg 元包（配合 psycopg-binary）
    "psycopg-binary": "latest",        # RDS PostgreSQL 驱动
    "redis": "latest",                 # 云 Redis
    "oss2": "2.7.0",                 # 阿里云 OSS SDK（tuna 无 wheel，自动回退 pypi.org）
    "aliyun-python-sdk-core": "latest",
    "aliyun-python-sdk-kms": "latest",
    "crcmod": "latest-sdist",        # 无 wheel；纯 Python 回退实现（免编译）
    "six": "latest",
    "requests": "latest",
    "urllib3": "latest",
    "charset-normalizer": "latest",
    "python-dateutil": "latest",
    "pycryptodome": "latest",        # oss2 依赖（Crypto）
    "jmespath": "latest",            # aliyun-python-sdk-core 依赖
    "cryptography": "latest",        # oss2 crypto 路径依赖
    "cffi": "latest",
    "pycparser": "latest",
    "tzdata": "latest",              # zoneinfo 数据库（RDS Asia/Shanghai 时区）
    "fakeredis": "latest",           # 测试：Redis 存储单元测试
    "sortedcontainers": "latest",    # fakeredis 依赖
    # ── RAG 编排：LangGraph（app/rag 查询流水线 + 摄取流水线）──────────────
    # 版本为 2026-08 在 Python 3.12 / win_amd64 下的完整解析结果
    "langgraph": "1.2.11",
    "langgraph-checkpoint": "4.2.0",
    "langgraph-prebuilt": "1.1.0",
    "langgraph-sdk": "0.4.4",
    "langchain-core": "1.6.1",
    "langchain-protocol": "0.0.19",
    "langsmith": "0.12.1",           # LangGraph 追踪（配置 LANGSMITH_API_KEY 后可接 LangSmith）
    "httpx2": "2.12.0",              # langsmith 使用的 httpx 分支（与应用 httpx 并存互不干扰）
    "httpcore2": "2.12.0",
    "orjson": "3.12.0",
    "ormsgpack": "1.12.2",
    "jsonpatch": "1.33",
    "jsonpointer": "3.1.1",
    "tenacity": "9.1.4",
    "requests-toolbelt": "1.0.0",
    "truststore": "0.10.4",
    "uuid-utils": "0.17.0",
    "xxhash": "4.0.1",
    "zstandard": "0.25.0",
    "distro": "1.9.0",
    "sniffio": "1.3.1",              # anyio 运行时依赖（此前缺失，随 langgraph 树补齐）
}

_WHEEL_RE = re.compile(r'href="([^"]+\.whl(?:#[^"]*)?)"')


def _version_tuple(filename: str) -> tuple:
    import re as _re

    m = _re.search(r"-(\d+(?:\.\d+)*(?:\.post\d+)?(?:rc\d+)?(?:a\d+)?(?:b\d+)?)", filename)
    if not m:
        return (0,)
    parts = _re.split(r"[.\-]", m.group(1))
    out = []
    for p in parts:
        if p.isdigit():
            out.append(int(p))
        else:
            out.append(0)
    return tuple(out)


def _rank(u: str) -> int:
    base = os.path.basename(u)
    if "cp312" in base and "win_amd64" in base:
        return 0
    if "py3-none-any" in base:
        return 1
    if "py2.py3-none-any" in base:
        return 2
    if "win_amd64" in base:
        return 3
    if "cp312" in base:
        return 4
    return 5


def _pick_candidate(name: str, version: str, html: str) -> str:
    links = [urljoin(BASE + name + "/", m.split("#")[0]) for m in _WHEEL_RE.findall(html)]
    if version == "latest":
        candidates = [
            u
            for u in links
            if (
                ("cp312" in u and "pypy" not in u)
                or "abi3" in u
                or "py3-none-any" in u
                or "py2.py3-none-any" in u
            )
        ]
        candidates.sort(key=lambda u: (_version_tuple(os.path.basename(u)), -_rank(u)))
        if not candidates:
            raise RuntimeError(f"未找到 {name} 的可用 wheel")
        return candidates[-1]
    candidates = [u for u in links if f"-{version}-" in os.path.basename(u)]
    candidates.sort(key=_rank)
    if not candidates:
        raise RuntimeError(f"未找到 {name}=={version} 的 wheel")
    return candidates[0]


def _open(url: str, timeout: int = 30):
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=_UA)
            return urllib.request.urlopen(req, timeout=timeout, context=_CTX)
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise last_err  # type: ignore[misc]


def fetch(name: str, version: str) -> str:
    page_url = BASE + name + "/"
    html = _open(page_url).read().decode("utf-8", "replace")
    if version == "latest-sdist":
        try:
            return _fetch_sdist(name, html)
        except RuntimeError:
            html = _open("https://pypi.org/simple/" + name + "/").read().decode("utf-8", "replace")
            return _fetch_sdist(name, html)
    try:
        wheel_url = _pick_candidate(name, version, html)
    except RuntimeError:
        # 镜像缺 wheel 时回退 pypi.org（如 oss2）
        fallback = "https://pypi.org/simple/"
        html = _open(fallback + name + "/").read().decode("utf-8", "replace")
        wheel_url = _pick_candidate(name, version, html)
    filename = os.path.basename(wheel_url)
    path = os.path.join(WHEELS_DIR, filename)
    if not os.path.exists(path):
        print(f"download {filename}")
        with _open(wheel_url, timeout=300) as resp, open(path, "wb") as fh:
            fh.write(resp.read())
    return path


def _fetch_sdist(name: str, html: str) -> str:
    """纯 Python 源码包：下载 sdist，把顶层包目录复制进 vendor（免构建）。

    解包目录放工作区内（沙箱限制 tempfile.mkdtemp 目录写入）。
    """
    import shutil
    import tarfile

    _SDIST_RE = re.compile(r'href="([^"]+(?:\.tar\.gz|\.zip)(?:#[^"]*)?)"')
    links = [urljoin(BASE + name + "/", m.split("#")[0]) for m in _SDIST_RE.findall(html)]
    sdists = [u for u in links if u.endswith((".tar.gz", ".zip")) and _version_tuple(os.path.basename(u))[0] != 0]
    sdists.sort(key=lambda u: _version_tuple(os.path.basename(u)))
    if not sdists:
        raise RuntimeError(f"未找到 {name} 的源码包")
    url = sdists[-1]
    filename = os.path.basename(url)
    path = os.path.join(WHEELS_DIR, filename)
    if not os.path.exists(path):
        print(f"download {filename}")
        with _open(url, timeout=300) as resp, open(path, "wb") as fh:
            fh.write(resp.read())

    tmp = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "sdist", name
    )
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        top_dir = ""
        if filename.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tf:
                top_dir = tf.getnames()[0].split("/")[0]
                # 手动逐文件写出（绕开 tarfile 解包附加的 chmod/utime 操作，沙箱会拒绝）
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if f"/{name}/" not in f"/{member.name}/":
                        continue
                    rel = member.name.split("/", 1)[1]
                    target_path = os.path.join(tmp, rel)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    source = tf.extractfile(member)
                    if source is not None:
                        with open(target_path, "wb") as out:
                            shutil.copyfileobj(source, out)
        else:
            import zipfile

            with zipfile.ZipFile(path) as zf:
                top_dir = zf.namelist()[0].split("/")[0]
                for info in zf.infolist():
                    if info.is_dir() or f"/{name}/" not in f"/{info.filename}/":
                        continue
                    rel = info.filename.split("/", 1)[1]
                    target_path = os.path.join(tmp, rel)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with zf.open(info) as src, open(target_path, "wb") as out:
                        shutil.copyfileobj(src, out)
        # 递归查找包目录（部分 sdist 如 crcmod 将包放在 python3/<name>/ 下，优先 python3 版本）
        matches = []
        for root, _dirs, files in os.walk(tmp):
            if os.path.basename(root) == name and "__init__.py" in files:
                matches.append(root)
        if not matches:
            raise RuntimeError(f"sdist 中未找到包目录 {name}/")
        matches.sort(key=lambda p: 0 if "/python3/" in p.replace("\\", "/") else 1)
        root = matches[0]
        target = os.path.join(TARGET, name)
        print(f"copy {name}/ -> vendor/")
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(root, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return os.path.join(TARGET, name)


def main() -> None:
    os.makedirs(WHEELS_DIR, exist_ok=True)
    os.makedirs(TARGET, exist_ok=True)
    for name, version in PACKAGES.items():
        path = fetch(name, version)
        if version == "latest-sdist":
            continue  # sdist 已在 _fetch_sdist 中复制进 vendor
        print(f"extract {os.path.basename(path)}")
        with zipfile.ZipFile(path) as zf:
            zf.extractall(TARGET)
    print(f"done -> {TARGET}")
    print('使用方式：$env:PYTHONPATH = "vendor;."')


if __name__ == "__main__":
    sys.exit(main())
