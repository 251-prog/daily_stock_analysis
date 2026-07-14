# -*- coding: utf-8 -*-

from unittest.mock import Mock, patch

from bot.commands.announcements import AnnouncementsCommand
from bot.dispatcher import CommandDispatcher
from bot.models import BotMessage, ChatType
from src.services.announcement_service import AnnouncementService


def _message(content: str) -> BotMessage:
    return BotMessage(
        platform="wecom",
        message_id="msg-1",
        user_id="owner",
        user_name="Owner",
        chat_id="group-1",
        chat_type=ChatType.GROUP,
        content=content,
        mentioned=True,
    )


def test_announcement_service_uses_display_time_and_extracts_earnings() -> None:
    listing = Mock()
    listing.raise_for_status.return_value = None
    listing.json.return_value = {
        "data": {
            "list": [
                {
                    "art_code": "AN1",
                    "title": "利通电子:2026年半年度业绩预增公告",
                    "display_time": "2026-07-14 15:33:43:386",
                    "notice_date": "2026-07-15 00:00:00",
                    "columns": [{"column_name": "业绩预告"}],
                }
            ]
        }
    }
    content = Mock()
    content.raise_for_status.return_value = None
    content.json.return_value = {
        "data": {
            "notice_content": (
                "预计2026年半年度实现归属于上市公司股东的净利润为65000万元到75000万元，"
                "同比增加1172.53%到1368.31%。"
                "三、本期业绩预增的主要原因 算力业务收入持续增长。"
                "四、风险提示"
            ),
            "attach_list": [{"attach_url": "https://example.test/original.pdf"}],
        }
    }

    with patch("src.services.announcement_service.datetime") as dt, patch(
        "src.services.announcement_service.requests.get",
        side_effect=[listing, content],
    ):
        dt.now.return_value = __import__("datetime").datetime(2026, 7, 14, 23, 50)
        dt.strptime.side_effect = __import__("datetime").datetime.strptime
        items = AnnouncementService().get_recent("603629", days=3, limit=1)

    assert len(items) == 1
    assert items[0]["display_time"].startswith("2026-07-14")
    assert "1172.53%" in items[0]["summary"]
    assert "算力业务" in items[0]["summary"]


def test_followup_announcement_question_uses_previous_stock_context() -> None:
    dispatcher = CommandDispatcher(admin_users=["owner"])
    dispatcher.register(AnnouncementsCommand())
    dispatcher._remember_explicit_stock(_message("分析 603629"))

    with patch(
        "bot.commands.announcements.AnnouncementService.get_recent",
        return_value=[
            {
                "title": "利通电子2026年半年度业绩预增公告",
                "display_time": "2026-07-14 15:33:43",
                "notice_date": "2026-07-15",
                "summary": "归母净利润同比大幅增长。",
                "url": "https://example.test/notice",
            }
        ],
    ):
        response = dispatcher.dispatch(_message("近3日无相关新闻或公告？今天不是刚发布业绩？"))

    assert "半年度业绩预增" in response.text
    assert "未调用 DeepSeek" in response.text
