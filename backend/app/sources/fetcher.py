"""数据管线抓取环节（爬取合规）。

- robots.txt 解析与遵守（Disallow/Allow 最长前缀匹配、Crawl-delay）；
- 礼貌抓取：自定义 UA、超时、频率控制；抓取前记录合规结论；
- 原始文件/网页快照经对象存储（生产）或本地目录（开发）保存，并写入 SourceDocument。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app.common.models import Source, SourceDocument

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "car-selection-crawler/0.1 (data collection; contact via site footer)"


@dataclass
class RobotsPolicy:
    """robots.txt 的最小实现（支持多 User-agent 组；最长前缀匹配，Allow 优先）。"""

    rules: list[tuple[str, str]] = field(default_factory=list)  # * 组 (directive, path)
    agent_rules: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # 特定 UA 组
    crawl_delay: float | None = None
    fetched_at: datetime | None = None
    fetch_failed: bool = False  # robots.txt 抓取失败（按默认允许处理，调用方须人工复核合规结论）

    @classmethod
    def parse(cls, content: str) -> "RobotsPolicy":
        policy = cls()
        current_agent = "*"
        for raw_line in content.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                current_agent = value or "*"
                continue
            if key in ("disallow", "allow") and value:
                policy.agent_rules.setdefault(current_agent, []).append((key, value))
                if current_agent == "*":
                    policy.rules.append((key, value))
            elif key == "crawl-delay":
                try:
                    policy.crawl_delay = float(value)
                except ValueError:
                    pass
        policy.fetched_at = datetime.now(timezone.utc)
        return policy

    @staticmethod
    def _eval(rules: list[tuple[str, str]], url_path: str) -> bool | None:
        """最长前缀匹配：Allow 优先于同长度 Disallow；无命中返回 None。"""
        best: tuple[int, str] | None = None
        for directive, rule_path in rules:
            if url_path.startswith(rule_path):
                # 同长度冲突时 Allow 优先（与 docstring/RFC9309 常见实现一致，评审 M-R3）
                if (
                    best is None
                    or len(rule_path) > best[0]
                    or (len(rule_path) == best[0] and directive == "allow")
                ):
                    best = (len(rule_path), directive)
        if best is None:
            return None
        return best[1] == "allow"

    @staticmethod
    def _match_agent(user_agent: str, group: str) -> bool:
        """RFC9309 产品令牌匹配：大小写不敏感、取首个 product token 前缀比较。"""
        token = user_agent.split("/", 1)[0].split(" ", 1)[0].strip().lower()
        return bool(token) and group.strip().lower() == token

    def allowed(self, url_path: str, user_agent: str | None = None) -> bool:
        """路径是否允许：特定 UA 组规则优先且独占（RFC9309），否则 * 组；未命中默认允许。"""
        if user_agent:
            for group, rules in self.agent_rules.items():
                if group != "*" and self._match_agent(user_agent, group):
                    verdict = self._eval(rules, url_path)
                    return True if verdict is None else verdict  # 特定组独占，不回退 * 组
        verdict = self._eval(self.agent_rules.get("*", []), url_path)
        return True if verdict is None else verdict


def fetch_robots(base_url: str, timeout: int = 10) -> RobotsPolicy:
    """抓取并解析 robots.txt；失败时返回 fetch_failed=True 的空策略（默认允许）。

    失败必须显式留痕（日志 + 标记）：静默 allow-all 会让调用方在 robots 不可达时
    无感知地抓取全站（评审 M-R3）。调用方应复核 fetch_failed 并人工确认合规结论。
    """
    import urllib.request

    robots_url = urljoin(base_url.rstrip("/") + "/", "robots.txt")
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return RobotsPolicy.parse(resp.read().decode("utf-8", "replace"))
    except Exception as err:  # noqa: BLE001 - robots 不可达时按默认允许，但必须留痕
        logger.warning("robots.txt 抓取失败（%s）：%s——按默认允许处理，须人工复核合规结论", robots_url, err)
        return RobotsPolicy(fetch_failed=True)


def record_snapshot(
    db,
    url: str,
    source_type: str,
    content_text: str | None = None,
    raw_object_path: str | None = None,
    *,
    brand_id: int | None = None,
    series_id: int | None = None,
    model_year_id: int | None = None,
    variant_id: int | None = None,
    source_name: str | None = None,
    page_or_section: str | None = None,
) -> SourceDocument:
    """把抓取/导入的原始文件快照记录为 SourceDocument（§8.2 每条数据带来源与时间）。"""
    source = None
    if source_name:
        from sqlalchemy import select

        source = db.scalar(select(Source).where(Source.name == source_name))
    doc = SourceDocument(
        brand_id=brand_id,
        series_id=series_id,
        model_year_id=model_year_id,
        variant_id=variant_id,
        source_id=source.id if source else None,
        url=url,
        source_type=source_type,
        page_or_section=page_or_section,
        content_text=content_text,
        raw_object_path=raw_object_path,
        crawled_at=datetime.now(timezone.utc),
        verification_status="unverified",
        credibility="medium",
    )
    db.add(doc)
    db.flush()
    return doc


def normalize_url(url: str) -> str:
    """URL 归一化：去 fragment、按站点内部路径保持原样（供 source_documents 去重）。"""
    parts = urlparse(url)
    return parts._replace(fragment="").geturl()
