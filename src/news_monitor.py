#!/usr/bin/env python3
"""Simple headline feed helpers for operator dashboards."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Dict, Iterable, List
from urllib.error import URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_REFRESH_SECONDS = 90.0
DEFAULT_USER_AGENT = "TrumpLiquidityNews/1.0"


def _normalise_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(text: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", text or "")
    return _normalise_whitespace(unescape(plain))


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_feed_bytes(payload: bytes, *, default_source: str) -> List[Dict[str, Any]]:
    """Parse RSS or Atom payloads into a common headline shape."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []

    entries: List[Dict[str, Any]] = []
    if root.tag.endswith("rss") or root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return []
        for item in channel.findall("item"):
            title = _strip_html(item.findtext("title") or "")
            if not title:
                continue
            description = _strip_html(item.findtext("description") or "")
            link = (item.findtext("link") or "").strip()
            published = _parse_datetime(item.findtext("pubDate"))
            source = _strip_html(item.findtext("source") or "") or default_source
            entries.append(
                {
                    "title": title,
                    "summary": description,
                    "link": link,
                    "source": source,
                    "published_utc": published.isoformat() if published else None,
                    "published_ts": published.timestamp() if published else 0.0,
                }
            )
        return entries

    if root.tag.endswith("feed"):
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        atom_entries = root.findall("atom:entry", ns)
        for item in atom_entries:
            title = _strip_html(item.findtext("atom:title", namespaces=ns) or "")
            if not title:
                continue
            summary = _strip_html(
                item.findtext("atom:summary", namespaces=ns)
                or item.findtext("atom:content", namespaces=ns)
                or ""
            )
            link_node = item.find("atom:link", ns)
            link = (link_node.get("href") if link_node is not None else "") or ""
            published = _parse_datetime(
                item.findtext("atom:published", namespaces=ns)
                or item.findtext("atom:updated", namespaces=ns)
            )
            entries.append(
                {
                    "title": title,
                    "summary": summary,
                    "link": link.strip(),
                    "source": default_source,
                    "published_utc": published.isoformat() if published else None,
                    "published_ts": published.timestamp() if published else 0.0,
                }
            )
        return entries

    return []


def _keyword_score(text: str, keywords: Iterable[str]) -> tuple[int, List[str]]:
    matched: List[str] = []
    lowered = text.lower()
    score = 0
    for keyword in keywords:
        key = keyword.lower().strip()
        if key and key in lowered:
            matched.append(keyword)
            score += 1
    return score, matched


def rank_news_items(
    items: Iterable[Dict[str, Any]],
    *,
    keywords: Iterable[str],
    limit: int = 10,
) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        keyword_hits, matched = _keyword_score(text, keywords)
        lowered = text.lower()
        trump_hit = any(term in lowered for term in ("trump", "donald trump"))
        iran_hit = any(term in lowered for term in ("iran", "iranian", "tehran"))
        if keyword_hits == 0 and not (trump_hit or iran_hit):
            continue
        score = keyword_hits + (3 if trump_hit else 0) + (3 if iran_hit else 0)
        ranked.append(
            {
                **item,
                "matched_terms": matched,
                "score": score,
            }
        )
    ranked.sort(
        key=lambda item: (
            int(item.get("score") or 0),
            float(item.get("published_ts") or 0.0),
        ),
        reverse=True,
    )
    return ranked[:limit]


def fetch_news_snapshot(
    sources: Iterable[Dict[str, str]],
    *,
    keywords: Iterable[str],
    limit: int = 10,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    all_items: List[Dict[str, Any]] = []
    errors: List[str] = []
    fetched_sources: List[str] = []
    for source in sources:
        name = str(source.get("name") or "Unknown source")
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
        except (URLError, TimeoutError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        fetched_sources.append(name)
        for item in parse_feed_bytes(payload, default_source=name):
            item["source"] = item.get("source") or name
            item["feed_name"] = name
            all_items.append(item)

    ranked = rank_news_items(all_items, keywords=keywords, limit=limit)
    status = "ok" if ranked else ("degraded" if fetched_sources else "offline")
    if not ranked and fetched_sources:
        status = "quiet"

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source_count": len(fetched_sources),
        "sources": fetched_sources,
        "errors": errors[:5],
        "items": ranked,
    }


class NewsCache:
    """Time-based cache for low-frequency headline fetches."""

    def __init__(
        self,
        *,
        sources: Iterable[Dict[str, str]],
        keywords: Iterable[str],
        limit: int = 10,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._sources = list(sources)
        self._keywords = list(keywords)
        self._limit = limit
        self._refresh_seconds = refresh_seconds
        self._payload: Dict[str, Any] | None = None
        self._built_at = 0.0
        self._lock = threading.Lock()

    def get_snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._payload is not None and now - self._built_at < self._refresh_seconds:
                return self._payload
        payload = fetch_news_snapshot(
            self._sources,
            keywords=self._keywords,
            limit=self._limit,
        )
        with self._lock:
            self._payload = payload
            self._built_at = now
        return payload


def parse_json_arg(raw: str | None, *, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
