# -*- coding: utf-8 -*-
"""Regression tests for low-cost bot lookup and confirmed watchlist updates."""

from unittest.mock import patch

from bot.commands.quote import QuoteCommand
from bot.commands.watchlist import WatchlistCommand
from bot.dispatcher import CommandDispatcher
from bot.models import BotMessage, ChatType


class FakeSystemConfigService:
    def __init__(self, stock_list: str = "603629,600879,603667") -> None:
        self.stock_list = stock_list
        self.config_version = "cfg-v1"

    def get_config(self, include_schema: bool = False) -> dict:
        return {
            "config_version": self.config_version,
            "items": [{"key": "STOCK_LIST", "value": self.stock_list}],
        }

    def update(self, **kwargs) -> None:
        self.stock_list = kwargs["items"][0]["value"]


def _message(content: str, user_id: str = "owner") -> BotMessage:
    return BotMessage(
        platform="wecom",
        message_id="msg-1",
        user_id=user_id,
        user_name="Owner",
        chat_id="chat-1",
        chat_type=ChatType.GROUP,
        content=content,
        mentioned=True,
    )


def test_bare_stock_code_routes_to_quote_without_nl_model_routing() -> None:
    dispatcher = CommandDispatcher(admin_users=["owner"])
    dispatcher.register(QuoteCommand())

    with patch("bot.commands.quote.StockService.get_realtime_quote", return_value={
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "current_price": 1500.0,
        "change": 10.0,
        "change_percent": 0.67,
        "high": 1508.0,
        "low": 1488.0,
    }) as get_quote:
        response = dispatcher.dispatch(_message("600519"))

    assert get_quote.called
    assert "贵州茅台" in response.text
    assert "未调用 AI" in response.text


def test_watchlist_add_requires_same_user_confirmation_before_persisting() -> None:
    service = FakeSystemConfigService()
    command = WatchlistCommand(service_factory=lambda: service)
    message = _message("自选 add 600519")

    add_response = command.execute(message, ["add", "600519"])
    assert "confirm 600519" in add_response.text
    assert "600519" not in service.stock_list

    confirm_response = command.execute(message, ["confirm", "600519"])
    assert "已将" in confirm_response.text
    assert service.stock_list.endswith(",600519")


def test_watchlist_confirmation_cannot_be_reused_by_another_user() -> None:
    service = FakeSystemConfigService()
    command = WatchlistCommand(service_factory=lambda: service)
    command.execute(_message("自选 add AAPL", user_id="owner"), ["add", "AAPL"])

    response = command.execute(_message("自选 confirm AAPL", user_id="other"), ["confirm", "AAPL"])

    assert "没有找到" in response.text
    assert "AAPL" not in service.stock_list
