"""抓取合规与快照记录测试（爬取合规）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import SourceDocument
from app.sources.fetcher import RobotsPolicy, normalize_url, record_snapshot

ROBOTS_SAMPLE = """
User-agent: *
Disallow: /admin
Disallow: /private/
Allow: /private/public
Crawl-delay: 2.5

User-agent: Googlebot
Disallow: /nothing
"""


def test_robots_parse_and_allowed():
    policy = RobotsPolicy.parse(ROBOTS_SAMPLE)
    assert policy.crawl_delay == 2.5
    assert policy.allowed("/admin/login") is False
    assert policy.allowed("/private/data") is False
    assert policy.allowed("/private/public") is True  # Allow 同长优先
    assert policy.allowed("/vehicles/1") is True
    assert policy.allowed("/administrator") is False  # RFC 9309 前缀匹配：/admin 命中 /administrator


def test_robots_specific_agent_group(monkeypatch):
    """面向特定 UA 的组优先于 * 组（评审 M12）。"""
    policy = RobotsPolicy.parse(
        """
        User-agent: *
        Disallow: /private/
        User-agent: car-selection-crawler
        Disallow: /private/
        Allow: /private/ok
        """
    )
    assert policy.allowed("/private/x") is False
    # 特定 UA 组内的 Allow 覆盖 * 组 Disallow
    assert policy.allowed("/private/ok", user_agent="car-selection-crawler") is True
    assert policy.allowed("/private/data", user_agent="car-selection-crawler") is False
    assert policy.allowed("/vehicles/1", user_agent="car-selection-crawler") is True


def test_robots_parse_empty_and_junk():
    policy = RobotsPolicy.parse("")
    assert policy.allowed("/anything") is True
    assert policy.crawl_delay is None

    policy = RobotsPolicy.parse("# 只有注释\n\nUser-agent: *\nDisallow:\n")
    assert policy.allowed("/x") is True


def test_normalize_url():
    assert normalize_url("https://example.com/a?x=1#frag") == "https://example.com/a?x=1"


def test_record_snapshot(db_session: Session):
    doc = record_snapshot(
        db_session,
        url="https://example.com/spec.pdf",
        source_type="official_doc",
        content_text="配置表正文",
        raw_object_path="snapshots/2026/spec-1.pdf",
        source_name="官方测试来源",
        series_id=1,
        page_or_section="P3",
    )
    db_session.commit()

    stored = db_session.scalar(select(SourceDocument).where(SourceDocument.id == doc.id))
    assert stored is not None
    assert stored.url == "https://example.com/spec.pdf"
    assert stored.content_text == "配置表正文"
    assert stored.verification_status == "unverified"
    assert stored.crawled_at is not None
    assert stored.source_id is None  # 来源名不存在时不外键报错，仅记录 null
