"""上线前检查清单（§20 合规逐项自查）。

检查项分三组：
- 环境与安全：数据库类型、建表开关、验证码回显、CORS、管理凭据、密钥类配置；
- 数据完整性：迁移版本、数据规模、品牌-车系/价格完整性；
- 在线接口（可选 --api）：健康检查、管理后台鉴权。

输出 PASS/WARN/FAIL 三级；存在 FAIL 时退出码 1。所有敏感值只显示「已设置/未设置」，绝不回显。
用法：
    python tools/checklist_prelaunch.py [--dev] [--api http://127.0.0.1:8000]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select, text  # noqa: E402

from app.common.config import get_settings  # noqa: E402
from app.common.database import get_session_factory  # noqa: E402
from app.common.models import Brand, OfficialPrice, SpecFact, VehicleSeries, VehicleVariant  # noqa: E402
from app.retrieval.config import MILVUS_TOKEN, MILVUS_URI, RETRIEVAL_BACKEND  # noqa: E402


def _head_revision() -> str:
    """扫描迁移目录：head = 未被任何文件引用为 down_revision 的版本。"""
    import re

    versions_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic", "versions")
    revisions: set[str] = set()
    downs: set[str] = set()
    for name in os.listdir(versions_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(versions_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        rev = re.search(r"^revision = ['\"]([^'\"]+)['\"]", text, re.M)
        down = re.search(r"^down_revision = ['\"]([^'\"]+)['\"]", text, re.M)
        if rev:
            revisions.add(rev.group(1))
        if down and down.group(1) != "None":
            downs.add(down.group(1))
    return sorted(revisions - downs)[0] if revisions - downs else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上线前检查清单")
    parser.add_argument("--dev", action="store_true", help="开发模式：放宽生产强制项（SQLite/建表/验证码回显）")
    parser.add_argument("--api", default="", help="在线接口检查（如 http://127.0.0.1:8000）")
    args = parser.parse_args(argv)

    results: list[tuple[str, str, str]] = []  # (level, item, note)
    fails = 0

    def add(level: str, item: str, note: str) -> None:
        nonlocal fails
        results.append((level, item, note))
        if level == "FAIL":
            fails += 1

    settings = get_settings()

    # ── 环境与安全 ──────────────────────────────────────────────
    db_url = settings.database_url or ""
    is_postgres = db_url.startswith("postgres")
    if not args.dev and not is_postgres:
        add("FAIL", "DATABASE_URL", "生产必须使用 PostgreSQL（当前非 postgres://，生产值见 .env.example）")
    elif not is_postgres:
        add("WARN", "DATABASE_URL", "当前为本地库（开发模式允许）")

    if not args.dev and settings.auto_create_tables:
        add("FAIL", "AUTO_CREATE_TABLES", "生产必须为 false（只走 Alembic 迁移）")
    elif settings.auto_create_tables:
        add("WARN", "AUTO_CREATE_TABLES", "开发模式允许自动建表")

    if not args.dev and settings.dev_echo_codes:
        add("FAIL", "DEV_ECHO_CODES", "生产必须为 false（验证码回显仅限开发）")
    elif settings.dev_echo_codes:
        add("WARN", "DEV_ECHO_CODES", "验证码回显开启（仅限开发环境）")

    cors = settings.cors_origins or []
    if "*" in cors:
        add("FAIL", "CORS_ORIGINS", "禁止 * 通配；只允许已备案前端域名")

    admin_token = settings.admin_api_token or ""
    if not args.dev and not admin_token:
        add("FAIL", "ADMIN_API_TOKEN", "生产必须设置管理后台凭据（≥16 位随机值，经环境变量注入）")
    elif not admin_token:
        add("WARN", "ADMIN_API_TOKEN", "未设置（管理接口将禁用；开发模式允许）")
    elif len(admin_token) < 16:
        add("WARN", "ADMIN_API_TOKEN", f"已设置但仅 {len(admin_token)} 位，建议 ≥16 位")

    if not settings.deepseek_api_key:
        add("WARN", "DEEPSEEK_API_KEY", "未设置：Agent 以确定性模板解释运行（原则 7，功能可用；建议配置）")
    if not settings.redis_url:
        add("WARN", "REDIS_URL", "未设置：会话/限流回退进程内存储（生产建议云 Redis）")
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        add("WARN", "SMTP", "未配置：验证码无法邮件投递（上线前必须配置）")

    if RETRIEVAL_BACKEND == "milvus":
        if MILVUS_URI and MILVUS_TOKEN:
            add("PASS", "RETRIEVAL_BACKEND", "milvus（Zilliz REST）已配置")
        else:
            add("FAIL", "RETRIEVAL_BACKEND", "选择了 milvus 但缺少 MILVUS_URI/MILVUS_TOKEN")
    else:
        add("WARN", "RETRIEVAL_BACKEND", "inmemory（本地 BM25 可用；生产建议 milvus 并先运行 build_retrieval_index.py）")

    # ── 数据完整性 ─────────────────────────────────────────────
    head = _head_revision()

    try:
        with get_session_factory()() as db:
            applied = db.scalar(text("SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1"))
    except Exception as err:  # noqa: BLE001
        add("FAIL", "数据库连接", f"无法连接：{type(err).__name__}")
        applied = None

    if applied is not None:
        if applied == head:
            add("PASS", "迁移版本", f"数据库 {applied} = 代码 head {head}")
        else:
            add("FAIL", "迁移版本", f"数据库 {applied} ≠ 代码 head {head}（先 alembic upgrade head）")

    try:
        with get_session_factory()() as db:
            n_brand = db.scalar(select(func.count()).select_from(Brand)) or 0
            n_series = db.scalar(select(func.count()).select_from(VehicleSeries)) or 0
            n_variant = db.scalar(select(func.count()).select_from(VehicleVariant)) or 0
            n_fact = db.scalar(select(func.count()).select_from(SpecFact)) or 0
            no_price = db.scalar(
                select(func.count())
                .select_from(VehicleVariant)
                .where(
                    VehicleVariant.status == "on_sale",
                    ~VehicleVariant.id.in_(
                        select(OfficialPrice.variant_id).where(OfficialPrice.effective_to.is_(None))
                    ),
                )
            ) or 0
            orphan_brands = db.scalar(
                select(func.count())
                .select_from(Brand)
                .where(
                    Brand.active_status == "active",
                    ~Brand.id.in_(select(VehicleSeries.brand_id)),
                )
            ) or 0
        if n_series < 100 or n_variant < 100:
            add("FAIL", "数据规模", f"品牌 {n_brand}/车系 {n_series}/款型 {n_variant}/事实 {n_fact}，规模异常")
        else:
            add("PASS", "数据规模", f"品牌 {n_brand}/车系 {n_series}/款型 {n_variant}/事实 {n_fact}")
        if no_price:
            add("WARN", "在售款型价格", f"{no_price} 个在售款型无指导价（展示为「官方资料未披露」）")
        if orphan_brands:
            add("WARN", "品牌完整性", f"{orphan_brands} 个活跃品牌无车系")
    except Exception as err:  # noqa: BLE001
        add("FAIL", "数据检查", f"查询失败：{type(err).__name__}")

    # ── 在线接口（可选）────────────────────────────────────────
    if args.api:
        import urllib.request

        base = args.api.rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=10) as resp:
                ok = resp.status == 200
            add("PASS" if ok else "FAIL", "健康检查", f"{base}/api/v1/health → {resp.status if ok else '失败'}")
        except Exception as err:  # noqa: BLE001
            add("FAIL", "健康检查", f"不可达：{type(err).__name__}")
        try:
            urllib.request.urlopen(f"{base}/api/v1/admin/stats", timeout=10)
            add("FAIL", "管理后台鉴权", "无凭据请求未返回 401")
        except urllib.error.HTTPError as err:
            add("PASS" if err.code == 401 else "FAIL", "管理后台鉴权", f"无凭据请求 → {err.code}")
        except Exception as err:  # noqa: BLE001
            add("WARN", "管理后台鉴权", f"跳过（{type(err).__name__}）")

    # ── 输出 ────────────────────────────────────────────────────
    for level, item, note in results:
        print(f"[{level:<4}] {item:<22} {note}")
    print(f"\n共 {len(results)} 项：FAIL {fails} 项。上线前必须清零 FAIL；WARN 逐项确认。")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
