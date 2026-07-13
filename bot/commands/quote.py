# -*- coding: utf-8 -*-
"""Low-cost real-time quote command for chat bots."""

from __future__ import annotations

import re
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from data_provider.base import normalize_stock_code
from src.services.stock_service import StockService


_STOCK_CODE_RE = re.compile(
    r"^(?:\d{6}|(?:SH|SZ|BJ)\d{6}|\d{6}\.(?:SH|SZ|SS|BJ)"
    r"|\d{1,5}\.HK|HK\d{1,5}|\d{5}|[A-Z]{1,5}(?:\.(?:US|[A-Z]))?)$",
    re.IGNORECASE,
)


def is_supported_stock_code(value: str) -> bool:
    """Return whether ``value`` is a supported stock-code form."""
    return bool(_STOCK_CODE_RE.fullmatch((value or "").strip()))


class QuoteCommand(BotCommand):
    """Return one latest quote without invoking the configured LLM."""

    @property
    def name(self) -> str:
        return "quote"

    @property
    def aliases(self) -> List[str]:
        return ["q", "查", "行情"]

    @property
    def description(self) -> str:
        return "查询最新行情（不调用 AI）"

    @property
    def usage(self) -> str:
        return "/quote <股票代码>"

    @property
    def admin_only(self) -> bool:
        """Prevent a group member from using a data-source request as a free API."""
        return True

    def validate_args(self, args: List[str]) -> Optional[str]:
        if len(args) != 1:
            return "请输入一个股票代码"
        if not is_supported_stock_code(args[0]):
            return "股票代码格式不正确（例如：600519、hk00700、AAPL）"
        return None

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        raw_code = args[0].strip()
        stock_code = normalize_stock_code(raw_code)
        quote = StockService().get_realtime_quote(stock_code)
        if not quote:
            return BotResponse.error_response(
                f"暂时无法获取 {raw_code.upper()} 的行情。请稍后再试，或使用“分析 {raw_code}”。"
            )

        name = quote.get("stock_name") or stock_code
        price = quote.get("current_price")
        change_pct = quote.get("change_percent")
        change_amount = quote.get("change")
        high = quote.get("high")
        low = quote.get("low")

        def _number(value, suffix: str = "") -> str:
            if value is None:
                return "--"
            try:
                return f"{float(value):,.2f}{suffix}"
            except (TypeError, ValueError):
                return f"{value}{suffix}"

        direction = "🟢" if (change_pct or 0) > 0 else "🔴" if (change_pct or 0) < 0 else "⚪"
        lines = [
            f"{direction} **{name}（{stock_code}）**",
            f"现价：**{_number(price)}**  |  涨跌：{_number(change_amount)}（{_number(change_pct, '%')}）",
            f"日内：{_number(low)} ～ {_number(high)}",
            "",
            "这是快速行情查询，未调用 AI。需要完整研究请发送：",
            f"`分析 {raw_code.upper()}`",
        ]
        return BotResponse.markdown_response("\n".join(lines))
