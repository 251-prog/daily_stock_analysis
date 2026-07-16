# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.services.wecom_watchlist_monitor import WeComWatchlistMonitor


class FakeAnnouncementService:
    def __init__(self) -> None:
        self.items = []

    def get_recent(self, *_args, **_kwargs):
        return list(self.items)


class FakeStockService:
    def __init__(self) -> None:
        self.change_pct = 4.0

    def get_realtime_quote(self, code):
        return {
            "stock_code": code,
            "stock_name": "测试股票",
            "current_price": 100.0,
            "change_percent": self.change_pct,
            "prev_close": 96.0,
            "open": 97.0,
            "high": 101.0,
            "low": 96.5,
            "volume": 2_000_000,
            "amount": 200_000_000,
        }

    def get_history_data(self, *_args, **_kwargs):
        start = datetime(2026, 5, 1)
        return {
            "data": [
                {
                    "date": (start + timedelta(days=index)).strftime("%Y-%m-%d"),
                    "open": float(value) - 0.5,
                    "high": float(value) + 1.0,
                    "low": float(value) - 1.0,
                    "close": float(value),
                    "volume": 1_000_000 + index * 10_000,
                }
                for index, value in enumerate(range(41, 101))
            ]
        }


def _config():
    return SimpleNamespace(
        stock_list=["603629"],
        wecom_aibot_allowed_chat_ids=["group-1"],
        wecom_watchlist_monitor_interval_minutes=5,
        wecom_watchlist_move_alert_pct=5.0,
        wecom_closing_brief_enabled=False,
        wecom_closing_brief_time="14:30",
    )


def test_monitor_seeds_old_announcements_then_sends_only_new_important_one(tmp_path) -> None:
    sent = []
    announcements = FakeAnnouncementService()
    announcements.items = [{"art_code": "old", "title": "普通公告"}]
    monitor = WeComWatchlistMonitor(
        _config(),
        lambda chat_id, content: sent.append((chat_id, content)) or True,
        state_path=tmp_path / "state.json",
        now_fn=lambda: datetime(2026, 7, 15, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        stock_service=FakeStockService(),
        announcement_service=announcements,
    )

    monitor.run_once()
    assert sent == []

    announcements.items.insert(
        0,
        {
            "art_code": "new",
            "title": "2026年半年度业绩预增公告",
            "display_time": "2026-07-15 08:30:00",
            "url": "https://example.test/new",
        },
    )
    monitor.run_once()

    assert len(sent) == 1
    assert "业绩预增" in sent[0][1]


def test_monitor_sends_one_abnormal_move_alert_during_trading_hours(tmp_path) -> None:
    sent = []
    stocks = FakeStockService()
    monitor = WeComWatchlistMonitor(
        _config(),
        lambda chat_id, content: sent.append((chat_id, content)) or True,
        state_path=tmp_path / "state.json",
        now_fn=lambda: datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        stock_service=stocks,
        announcement_service=FakeAnnouncementService(),
    )

    monitor.run_once()
    assert sent == []
    stocks.change_pct = 6.0
    monitor.run_once()
    monitor.run_once()

    assert len(sent) == 1
    assert "+6.00%" in sent[0][1]


def test_monitor_sends_closing_brief_once_after_1430(tmp_path) -> None:
    sent = []
    config = _config()
    config.wecom_closing_brief_enabled = True
    monitor = WeComWatchlistMonitor(
        config,
        lambda chat_id, content: sent.append((chat_id, content)) or True,
        state_path=tmp_path / "state.json",
        now_fn=lambda: datetime(
            2026, 7, 15, 14, 32, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
        stock_service=FakeStockService(),
        announcement_service=FakeAnnouncementService(),
    )

    with patch("src.services.wecom_watchlist_monitor.is_market_open", return_value=True):
        monitor.run_once()
        monitor.run_once()

    closing_messages = [content for _, content in sent if "尾盘30分钟简报" in content]
    assert len(closing_messages) == 1
    assert "测试股票（603629）" in closing_messages[0]
    assert "持仓者" in closing_messages[0]
    assert "空仓者" in closing_messages[0]
    assert "KDJ" in closing_messages[0]


def test_monitor_does_not_send_closing_brief_before_1430(tmp_path) -> None:
    sent = []
    config = _config()
    config.wecom_closing_brief_enabled = True
    monitor = WeComWatchlistMonitor(
        config,
        lambda chat_id, content: sent.append((chat_id, content)) or True,
        state_path=tmp_path / "state.json",
        now_fn=lambda: datetime(
            2026, 7, 15, 14, 29, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
        stock_service=FakeStockService(),
        announcement_service=FakeAnnouncementService(),
    )

    with patch("src.services.wecom_watchlist_monitor.is_market_open", return_value=True):
        monitor.run_once()

    assert not any("尾盘30分钟简报" in content for _, content in sent)
