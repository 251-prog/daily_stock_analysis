# -*- coding: utf-8 -*-
"""Frame parsing tests for the optional Enterprise WeChat intelligent-bot adapter."""

from bot.models import ChatType
from bot.platforms.wecom_aibot import WeComAiBotClient


def test_wecom_aibot_parses_group_text_frame() -> None:
    message = WeComAiBotClient._frame_to_message(
        {
            "req_id": "request-1",
            "body": {
                "msgid": "message-1",
                "chatid": "group-1",
                "chattype": "group",
                "from": {"userid": "user-1", "alias": "测试用户"},
                "text": {"content": " 600519 "},
            },
        }
    )

    assert message is not None
    assert message.user_id == "user-1"
    assert message.chat_id == "group-1"
    assert message.chat_type == ChatType.GROUP
    assert message.content == "600519"


def test_wecom_aibot_ignores_non_text_frame() -> None:
    assert WeComAiBotClient._frame_to_message({"body": {"image": {"url": "x"}}}) is None
