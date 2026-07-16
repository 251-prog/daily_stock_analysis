# -*- coding: utf-8 -*-
"""Direct A-share announcement lookup with no LLM dependency."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

import requests


class AnnouncementService:
    """Read recent company announcements from Eastmoney's public notice API.

    The endpoint republishes exchange filings and exposes the original PDF.
    It is used as a deterministic fallback when general web-search providers
    are rate-limited or publish an after-hours filing with a next-day notice
    date.
    """

    LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    DETAIL_URL = "https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    @staticmethod
    def _parse_display_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        # The API appends milliseconds with a second colon, e.g.
        # ``2026-07-14 15:33:43:386``.
        text = re.sub(r":\d{3}$", "", text)
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _collapse(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        # PDF text extraction often inserts a line-break space between two
        # Chinese characters ("实 \u73b0", "损益 \u7684").  Removing only those
        # spaces preserves readable separation around numbers and Latin text.
        return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)

    def _fetch_content(self, art_code: str) -> Dict[str, Any]:
        response = requests.get(
            self.CONTENT_URL,
            params={
                "art_code": art_code,
                "client_source": "web",
                "page_index": "1",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return (response.json().get("data") or {})

    @classmethod
    def _extract_earnings_summary(cls, content: str) -> str:
        text = cls._collapse(content)
        if not text:
            return ""

        points: List[str] = []
        for sentence in re.split(r"[。；]", text):
            sentence = sentence.strip(" ：:；;")
            if not sentence:
                continue
            if "预计" in sentence and "净利润为" in sentence and "同比" in sentence:
                points.append(sentence)
            if len(points) >= 2:
                break

        reason_match = re.search(
            r"本期业绩预增的主要原因\s*(.+?)(?:四、风险提示|五、其他说明事项)",
            text,
        )
        if reason_match:
            reason = reason_match.group(1).strip(" ：:；;")
            if reason:
                points.append(f"主要原因：{reason[:360]}")

        return "\n".join(points[:3])

    def get_recent(
        self,
        stock_code: str,
        *,
        days: int = 3,
        limit: int = 5,
        include_content: bool = True,
    ) -> List[Dict[str, Any]]:
        code = str(stock_code or "").strip().upper()
        if not re.fullmatch(r"\d{6}", code):
            return []

        response = requests.get(
            self.LIST_URL,
            params={
                "sr": "-1",
                "page_size": str(max(10, limit * 2)),
                "page_index": "1",
                "ann_type": "A",
                "client_source": "web",
                "f_node": "0",
                "s_node": "0",
                "stock_list": code,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        raw_items = ((response.json().get("data") or {}).get("list") or [])
        cutoff = datetime.now() - timedelta(days=max(1, days))
        results: List[Dict[str, Any]] = []

        for raw in raw_items:
            display_dt = self._parse_display_time(raw.get("display_time"))
            if display_dt is not None and display_dt < cutoff:
                continue
            art_code = str(raw.get("art_code") or "").strip()
            if not art_code:
                continue
            columns = raw.get("columns") or []
            category = str(columns[0].get("column_name") or "") if columns else ""
            item: Dict[str, Any] = {
                "code": code,
                "title": self._collapse(raw.get("title")),
                "category": category,
                "display_time": display_dt.isoformat(sep=" ") if display_dt else "",
                "notice_date": str(raw.get("notice_date") or "")[:10],
                "art_code": art_code,
                "url": self.DETAIL_URL.format(code=code, art_code=art_code),
                "summary": "",
                "original_pdf": "",
            }

            is_earnings = any(
                keyword in f"{item['title']} {category}"
                for keyword in ("业绩", "年报", "半年报", "季报", "财务报告")
            )
            if include_content and is_earnings:
                try:
                    content_data = self._fetch_content(art_code)
                    item["summary"] = self._extract_earnings_summary(
                        content_data.get("notice_content") or ""
                    )
                    attachments = content_data.get("attach_list") or []
                    if attachments:
                        item["original_pdf"] = str(attachments[0].get("attach_url") or "")
                except (requests.RequestException, ValueError, TypeError):
                    # The title and detail link remain useful if the content
                    # endpoint is temporarily unavailable.
                    pass

            results.append(item)
            if len(results) >= limit:
                break

        return results
