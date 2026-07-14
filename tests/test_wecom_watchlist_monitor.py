# -*- coding: utf-8 -*-

from datetime import datetime
from types import SimpleNamespace
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
        }

    def get_history_data(self, *_args, **_kwargs):
        return {"data": [{"close": float(value)} for value in range(81, 101)]}


def _config():
    return SimpleNamespace(
        stock_list=["603629"],
        wecom_aibot_allowed_chat_ids=["group-1"],
        wecom_watchlist_monitor_interval_minutes=5,
        wecom_watchlist_move_alert_pct=5.0,
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

