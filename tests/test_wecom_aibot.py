# -*- coding: utf-8 -*-
"""Tests for the optional Enterprise WeChat intelligent-bot adapter."""

from unittest.mock import AsyncMock

import pytest

from bot.models import ChatType
from bot.platforms.wecom_aibot import WeComAiBotClient, _format_analysis_followup


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


def test_wecom_aibot_strips_leading_group_mention() -> None:
    message = WeComAiBotClient._frame_to_message(
        {
            "body": {
                "msgid": "message-2",
                "chatid": "group-1",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "text": {"content": "@股票研究助手 603629"},
            }
        }
    )

    assert message is not None
    assert message.content == "603629"


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


def test_wecom_analysis_followup_contains_reasoning_and_kdj() -> None:
    text = _format_analysis_followup(
        {
            "code": "603629",
            "result": {
                "code": "603629",
                "name": "利通电子",
                "sentiment_score": 25,
                "operation_advice": "减仓",
                "trend_prediction": "强烈看空",
                "analysis_summary": "均线与量价结构同时转弱。",
                "buy_reason": "短期均线空头排列，且放量下跌。",
                "kdj_k": 21.0,
                "kdj_d": 30.0,
                "kdj_j": 3.0,
                "kdj_signal": "KDJ 处于低位但尚未金叉。",
                "news_summary": "暂无足以改变结论的重大公告。",
                "risk_warning": "需防范继续破位。",
            },
        }
    )

    assert "分析思路" in text
    assert "K 21.00" in text
    assert "影响判断的最新消息" in text


@pytest.mark.anyio
async def test_wecom_aibot_proactively_sends_completed_analysis(monkeypatch) -> None:
    class CompletedTaskService:
        @staticmethod
        def get_task_status(task_id: str) -> dict:
            assert task_id == "task-1"
            return {
                "status": "completed",
                "code": "603629",
                "result": {
                    "code": "603629",
                    "name": "利通电子",
                    "sentiment_score": 50,
                    "operation_advice": "观望",
                    "trend_prediction": "震荡",
                    "analysis_summary": "等待趋势确认。",
                },
            }

    monkeypatch.setattr(
        "src.services.task_service.get_task_service",
        lambda: CompletedTaskService(),
    )
    client = object.__new__(WeComAiBotClient)
    client._client = AsyncMock()
    client._running = True

    await client._watch_analysis_task("task-1", "group-1", poll_interval=0)

    client._client.send_message.assert_awaited_once()
    args, _ = client._client.send_message.await_args
    assert args[0] == "group-1"
    assert args[1]["msgtype"] == "markdown"
    assert "利通电子" in args[1]["markdown"]["content"]
