"""月度销量自动获取工具测试（tools/fetch_sales_scheduled.py）。

策略验证：
- 库内已有目标月数据 → 不抓取直接成功（幂等）；
- 门户未发布目标月 → 不导入、返回 0（预期状态），明日重试；滞后超阈值告警；
- 抓取/导入失败 → 退出码 2；
- 导入成功后校验库内行数，校验失败 → 退出码 2。

说明：本机受限沙箱会拒绝枚举 pytest 临时目录（tmp_path 不可用），
日志文件统一写到 tests/.tmp（已 gitignore）。
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import fetch_sales_scheduled as fss  # noqa: E402

from tests.seed import make_brand, make_sales, make_series, make_source  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / ".tmp"


def _log_path(name: str) -> Path:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = LOG_DIR / f"sales_scheduled_{name}.log"
    path.unlink(missing_ok=True)  # 每次运行从干净日志开始
    return path


def _factory(db_session: Session) -> sessionmaker:
    return sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)


def test_days_since_month_end():
    assert fss.days_since_month_end("2026-08", date(2026, 9, 2)) == 2
    assert fss.days_since_month_end("2026-08", date(2026, 8, 20)) == -11
    assert fss.days_since_month_end("2025-12", date(2026, 1, 5)) == 5


def test_ready_month_skips_fetch(db_session: Session, monkeypatch):
    source = make_source(db_session)
    brand = make_brand(db_session, source=source)
    series = make_series(db_session, brand, source=source)
    make_sales(db_session, series, "2026-08", 100, source=source)
    db_session.commit()

    def boom(**kwargs):
        raise AssertionError("库内已有目标月数据时不应再抓取")

    monkeypatch.setattr(fss, "fetch_and_import_month", boom)
    log_path = _log_path("ready")
    rc = fss.main(["--month", "2026-08", "--log", str(log_path)], session_factory=_factory(db_session))
    assert rc == 0
    assert "已就绪" in log_path.read_text(encoding="utf-8")


def test_not_ready_no_import_and_alert(db_session: Session, monkeypatch):
    """门户仍公布上一月：不导入、退出 0；滞后 ≥ warn-days 时写告警。"""
    calls: list[dict] = []

    def fake_fetch(month, **kwargs):
        calls.append(kwargs)
        return {
            "requested_month": "2026-08",
            "month": "2026-07",
            "rows": 20,
            "imported": False,
            "errors": [],
            "created": 0,
            "updated": 0,
            "skipped_reason": "门户尚未发布 2026-08 榜单（当前公布 2026-07）",
        }

    monkeypatch.setattr(fss, "fetch_and_import_month", fake_fetch)
    monkeypatch.setattr(fss, "days_since_month_end", lambda month, today=None: 20)

    log_path = _log_path("unready")
    rc = fss.main(
        ["--month", "2026-08", "--warn-days", "15", "--log", str(log_path)],
        session_factory=_factory(db_session),
    )
    assert rc == 0
    assert calls and calls[0]["require_month_match"] is True
    assert calls[0]["do_import"] is True
    text = log_path.read_text(encoding="utf-8")
    assert "未就绪" in text
    assert "告警" in text and "20 天" in text


def test_success_verifies_rows_in_db(db_session: Session, monkeypatch):
    factory = _factory(db_session)
    source = make_source(db_session)
    brand = make_brand(db_session, source=source)
    series = make_series(db_session, brand, source=source)
    db_session.commit()

    def fake_fetch(month, **kwargs):
        # 模拟 import_catalog 落库效果：目标月出现销量行
        with factory() as db2:
            make_sales(db2, series, "2026-08", 321, source=source)
            db2.commit()
        return {
            "requested_month": "2026-08",
            "month": "2026-08",
            "rows": 20,
            "imported": True,
            "errors": [],
            "created": 1,
            "updated": 0,
            "skipped_reason": None,
        }

    monkeypatch.setattr(fss, "fetch_and_import_month", fake_fetch)
    log_path = _log_path("ok")
    rc = fss.main(["--month", "2026-08", "--log", str(log_path)], session_factory=factory)
    assert rc == 0
    assert "成功" in log_path.read_text(encoding="utf-8")


def test_fetch_error_returns_exit_2(db_session: Session, monkeypatch):
    def boom(month, **kwargs):
        raise RuntimeError("网络中断")

    monkeypatch.setattr(fss, "fetch_and_import_month", boom)
    log_path = _log_path("err")
    rc = fss.main(["--month", "2026-08", "--log", str(log_path)], session_factory=_factory(db_session))
    assert rc == 2
    assert "失败" in log_path.read_text(encoding="utf-8")


def test_import_report_errors_return_exit_2(db_session: Session, monkeypatch):
    def fake_fetch(month, **kwargs):
        return {
            "requested_month": "2026-08",
            "month": "2026-08",
            "rows": 20,
            "imported": True,
            "errors": ["系列 X 缺少 external_id"],
            "created": 0,
            "updated": 0,
            "skipped_reason": None,
        }

    monkeypatch.setattr(fss, "fetch_and_import_month", fake_fetch)
    log_path = _log_path("importerr")
    rc = fss.main(
        ["--month", "2026-08", "--log", str(log_path)],
        session_factory=_factory(db_session),
    )
    assert rc == 2
