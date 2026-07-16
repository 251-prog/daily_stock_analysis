# -*- coding: utf-8 -*-

from types import SimpleNamespace
from unittest.mock import patch

from bot.commands.chat import ChatCommand
from bot.commands.announcements import AnnouncementsCommand
from bot.dispatcher import CommandDispatcher
from bot.models import BotMessage, ChatType
from src.agent.llm_adapter import LLMResponse
from src.services.lightweight_stock_qa_service import LightweightStockQaService


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


def test_lightweight_qa_uses_one_grounded_model_call() -> None:
    captured = {}

    def fake_call(_adapter, messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return LLMResponse(content="结论：业绩预告已披露，需核对原文。", provider="deepseek")

    with patch(
        "src.services.lightweight_stock_qa_service.StockService.get_realtime_quote",
        return_value={"stock_code": "603629", "current_price": 100.0},
    ), patch(
        "src.services.lightweight_stock_qa_service.AnnouncementService.get_recent",
        return_value=[
            {
                "title": "2026年半年度业绩预增公告",
                "display_time": "2026-07-14 20:00:00",
                "url": "https://example.test/notice",
            }
        ],
    ), patch(
        "src.services.task_service.get_task_service"
    ) as task_service, patch(
        "src.services.lightweight_stock_qa_service.LLMToolAdapter.call_text",
        new=fake_call,
    ):
        task_service.return_value.get_analysis_history.return_value = []
        answer = LightweightStockQaService().answer(
            question="这次业绩怎么看？",
            session_id="session-test-grounded",
            stock_code="603629",
            config=SimpleNamespace(),
        )

    assert "业绩预告" in answer
    assert "公告来源" in answer
    evidence = captured["messages"][1]["content"]
    assert "603629" in evidence
    assert "https://example.test/notice" in evidence
    assert captured["kwargs"]["max_tokens"] == 900


def test_dispatcher_routes_arbitrary_stock_name_to_lightweight_qa() -> None:
    dispatcher = CommandDispatcher(admin_users=["owner"])
    dispatcher.register(ChatCommand())
    dispatcher.register(AnnouncementsCommand())

    with patch("bot.commands.chat.get_config", return_value=SimpleNamespace(agent_mode=False)), patch(
        "src.services.lightweight_stock_qa_service.LightweightStockQaService.answer",
        return_value="贵州茅台研究回答",
    ) as answer:
        response = dispatcher.dispatch(_message("贵州茅台最近怎么看？"))

    assert "贵州茅台研究回答" in response.text
    assert answer.call_args.kwargs["stock_code"] == "600519"


def test_command_like_natural_question_falls_back_to_lightweight_qa() -> None:
    dispatcher = CommandDispatcher(admin_users=["owner"])
    dispatcher.register(ChatCommand())
    dispatcher.register(AnnouncementsCommand())

    message = _message("业绩怎么看？")
    dispatcher._last_stock_by_scope[dispatcher._context_scope(message)] = "603629"
    with patch("bot.commands.chat.get_config", return_value=SimpleNamespace(agent_mode=False)), patch(
        "src.services.lightweight_stock_qa_service.LightweightStockQaService.answer",
        return_value="业绩解读回答",
    ) as answer:
        response = dispatcher.dispatch(message)

    assert "业绩解读回答" in response.text
    assert answer.call_args.kwargs["stock_code"] == "603629"
