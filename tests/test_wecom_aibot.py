# -*- coding: utf-8 -*-
"""Tests for the optional Enterprise WeChat intelligent-bot adapter."""

from unittest.mock import AsyncMock

import pytest

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


@pytest.mark.anyio
async def test_wecom_aibot_replies_with_completed_stream() -> None:
    client = object.__new__(WeComAiBotClient)
    client._client = AsyncMock()
    frame = {"headers": {"req_id": "request-1"}, "body": {"msgid": "message-1"}}

    await client._reply_text(frame, "行情回复")

    client._client.reply_stream.assert_awaited_once()
    args, kwargs = client._client.reply_stream.await_args
    assert args == (frame,)
    assert kwargs["content"] == "行情回复"
    assert kwargs["finish"] is True
    assert kwargs["stream_id"].startswith("dsa-")
