# -*- coding: utf-8 -*-
"""Low-cost, evidence-grounded stock Q&A for private bot deployments."""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from data_provider.base import canonical_stock_code
from src.agent.llm_adapter import LLMToolAdapter
from src.services.announcement_service import AnnouncementService
from src.services.stock_service import StockService

logger = logging.getLogger(__name__)

_MAX_HISTORY_MESSAGES = 6
_history_lock = threading.Lock()
_history_by_session: Dict[str, Deque[Dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=_MAX_HISTORY_MESSAGES)
)


class LightweightStockQaService:
    """Answer a stock-research question with one bounded LLM call.

    This intentionally is not the full Agent runtime: it exposes no tools to
    the model, performs no autonomous planning, and never mutates a watchlist.
    Market facts are fetched before the call and supplied as read-only context.
    """

    SYSTEM_PROMPT = """你是个人股票研究问答助手。请只在股票、市场、公告、财报和投资研究教育范围内回答。
规则：
1. 只把“证据数据”中的内容当作实时或最新事实；不要凭模型记忆补写价格、公告、财务数字或新闻。
2. 若证据不足，明确说“当前数据未提供/暂时无法确认”，并说明应去哪里复核。
3. 回答先给结论，再按“证据 → 推理 → 风险/待确认项”展开；这是分析思路摘要，不输出隐藏思维链。
4. 区分事实、推断和假设。涉及公告时保留日期与原文链接。
5. 不承诺收益，不给自动交易指令；可以解释指标、情景和观察条件。
6. 用户临时查询任何合法股票都可以回答，但不得声称它已加入自选股。
7. 使用简洁自然的中文，适合企业微信群阅读，通常不超过700字。
8. 如果问题与股票研究无关，简短说明服务范围并请用户换一个股票或市场问题。"""

    @staticmethod
    def _compact_quote(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not quote:
            return {}
        keys = (
            "stock_code",
            "stock_name",
            "current_price",
            "change",
            "change_percent",
            "open",
            "high",
            "low",
            "prev_close",
            "volume",
            "amount",
            "update_time",
        )
        return {key: quote.get(key) for key in keys if quote.get(key) is not None}

    @staticmethod
    def _compact_history(record: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "code",
            "name",
            "analysis_date",
            "created_at",
            "operation_advice",
            "trend_prediction",
            "sentiment_score",
            "analysis_summary",
            "technical_analysis",
            "news_summary",
            "risk_warning",
            "kdj_k",
            "kdj_d",
            "kdj_j",
            "kdj_signal",
        )
        return {key: record.get(key) for key in keys if record.get(key) not in (None, "")}

    def _build_evidence(self, stock_code: Optional[str]) -> Dict[str, Any]:
        if not stock_code:
            return {
                "stock_code": None,
                "notice": "本轮未识别到具体股票；只能回答通用研究问题，不能提供实时个股事实。",
            }

        code = canonical_stock_code(stock_code)
        evidence: Dict[str, Any] = {"stock_code": code}

        try:
            evidence["realtime_quote"] = self._compact_quote(
                StockService().get_realtime_quote(code)
            )
        except Exception as exc:
            logger.warning("[LightweightQA] quote lookup failed for %s: %s", code, exc)
            evidence["realtime_quote"] = {}

        if code.isdigit() and len(code) == 6:
            try:
                announcements = AnnouncementService().get_recent(
                    code, days=7, limit=5, include_content=True
                )
                evidence["company_announcements_last_7d"] = announcements
            except Exception as exc:
                logger.warning(
                    "[LightweightQA] announcement lookup failed for %s: %s", code, exc
                )
                evidence["company_announcements_last_7d"] = []

        try:
            from src.services.task_service import get_task_service

            history = get_task_service().get_analysis_history(code=code, days=14, limit=1)
            evidence["latest_local_report"] = (
                self._compact_history(history[0]) if history else {}
            )
        except Exception as exc:
            logger.debug("[LightweightQA] history lookup failed for %s: %s", code, exc)
            evidence["latest_local_report"] = {}

        return evidence

    def answer(
        self,
        *,
        question: str,
        session_id: str,
        stock_code: Optional[str] = None,
        config: Any = None,
    ) -> str:
        evidence = self._build_evidence(stock_code)
        with _history_lock:
            history: List[Dict[str, str]] = list(_history_by_session[session_id])

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "system",
                "content": "本轮证据数据（JSON）：\n"
                + json.dumps(evidence, ensure_ascii=False, default=str),
            },
            *history,
            {"role": "user", "content": question},
        ]

        response = LLMToolAdapter(config).call_text(
            messages,
            temperature=0.2,
            max_tokens=900,
            timeout=75,
        )
        content = str(response.content or "").strip()
        if not content or response.provider == "error" or content.startswith("All LLM models failed"):
            raise RuntimeError("模型暂时不可用")

        announcements = evidence.get("company_announcements_last_7d") or []
        source_lines = []
        for item in announcements[:3]:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "公告原文").strip()
            if url and url not in content:
                source_lines.append(f"- [{title}]({url})")
        if source_lines:
            content = f"{content}\n\n**公告来源**\n" + "\n".join(source_lines)

        with _history_lock:
            session_history = _history_by_session[session_id]
            session_history.append({"role": "user", "content": question[:800]})
            session_history.append({"role": "assistant", "content": content[:1600]})
        return content
