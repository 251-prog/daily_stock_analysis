# -*- coding: utf-8 -*-
"""Enterprise WeCom intelligent-bot adapter using a WebSocket long connection.

The ordinary Enterprise WeChat group webhook is outbound-only.  This adapter
uses the separate "智能机器人 / API 模式" channel so a private computer can
receive group messages without exposing an HTTP callback endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from bot.models import BotMessage, ChatType, Platform

logger = logging.getLogger(__name__)
_LEADING_MENTION_RE = re.compile(r"^\s*@\S+\s+")


def _clean_report_text(value: Any, limit: int) -> str:
    """Collapse model prose for a compact group-chat follow-up."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _format_analysis_followup(task: Dict[str, Any]) -> str:
    """Build a concise research result without presenting it as trading advice."""
    result = task.get("result") or {}
    code = str(result.get("code") or task.get("code") or "--")
    name = str(result.get("name") or code)
    score = result.get("sentiment_score")
    score_text = f"{score}/100" if score is not None else "--"
    conclusion = _clean_report_text(result.get("analysis_summary"), 520)
    reasoning = _clean_report_text(
        result.get("buy_reason") or result.get("technical_analysis"), 520
    )
    news = _clean_report_text(result.get("news_summary"), 320)
    risk = _clean_report_text(result.get("risk_warning"), 320)

    lines = [
        f"📊 **{name} ({code}) 研究结果**",
        "",
        f"> 结论：**{result.get('operation_advice') or '—'}** ｜ "
        f"趋势：{result.get('trend_prediction') or '—'} ｜ 评分：{score_text}",
    ]
    if conclusion:
        lines.extend(["", f"**核心判断**\n{conclusion}"])
    if reasoning and reasoning != conclusion:
        lines.extend(["", f"**分析思路**\n{reasoning}"])

    kdj_values = [result.get("kdj_k"), result.get("kdj_d"), result.get("kdj_j")]
    if all(value is not None for value in kdj_values):
        lines.extend(
            [
                "",
                f"**KDJ(9,3,3)**\nK {kdj_values[0]:.2f} ｜ D {kdj_values[1]:.2f} ｜ "
                f"J {kdj_values[2]:.2f}",
            ]
        )
        kdj_signal = _clean_report_text(result.get("kdj_signal"), 220)
        if kdj_signal:
            lines.append(kdj_signal)
    if news:
        lines.extend(["", f"**影响判断的最新消息**\n{news}"])
    if risk:
        lines.extend(["", f"**主要风险**\n{risk}"])
    lines.extend(["", "_仅供个人研究，不构成交易建议。价格、公告与财报请以官方信息为准。_"])
    return "\n".join(lines)

try:
    from wecom_aibot_sdk import WSClient

    WECOM_AIBOT_SDK_AVAILABLE = True
except ImportError:
    WSClient = None
    WECOM_AIBOT_SDK_AVAILABLE = False


class WeComAiBotClient:
    """Run an Enterprise WeChat intelligent bot in a background thread."""

    def __init__(
        self,
        bot_id: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> None:
        if not WECOM_AIBOT_SDK_AVAILABLE:
            raise ImportError("wecom-aibot-sdk 未安装")

        from src.config import get_config

        config = get_config()
        self._bot_id = bot_id or getattr(config, "wecom_aibot_id", None)
        self._secret = secret or getattr(config, "wecom_aibot_secret", None)
        self._admin_users = set(getattr(config, "bot_admin_users", []) or [])
        self._allowed_chat_ids = set(getattr(config, "wecom_aibot_allowed_chat_ids", []) or [])
        if not self._bot_id or not self._secret:
            raise ValueError("企业微信智能机器人需要 WECOM_AIBOT_ID 和 WECOM_AIBOT_SECRET")

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._followup_tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    def _frame_to_message(frame: Dict[str, Any]) -> Optional[BotMessage]:
        """Translate the documented intelligent-bot text frame into BotMessage."""
        body = frame.get("body") or {}
        text = ((body.get("text") or {}).get("content") or "").strip()
        if not text:
            return None

        # Group callbacks may retain the visible ``@机器人`` prefix in
        # ``text.content``.  The dispatcher must see only the user's command;
        # otherwise a bare stock code falls through to the help response.
        text_without_mention = _LEADING_MENTION_RE.sub("", text, count=1).strip()
        if text_without_mention:
            text = text_without_mention

        sender = body.get("from") or {}
        user_id = str(sender.get("userid") or sender.get("user_id") or "")
        if not user_id:
            logger.warning("[WeCom AI Bot] 收到缺少用户 ID 的消息，已忽略")
            return None

        chat_id = str(body.get("chatid") or body.get("chat_id") or user_id)
        chat_type_value = str(body.get("chattype") or body.get("chat_type") or "").lower()
        chat_type = ChatType.GROUP if chat_type_value == "group" else ChatType.PRIVATE
        return BotMessage(
            platform=Platform.WECOM.value,
            message_id=str(body.get("msgid") or body.get("msg_id") or frame.get("req_id") or ""),
            user_id=user_id,
            user_name=str(sender.get("alias") or sender.get("name") or user_id),
            chat_id=chat_id,
            chat_type=chat_type,
            content=text,
            raw_content=text,
            # The intelligent-bot channel only forwards messages addressed to
            # the bot in a group; marking this true keeps help behavior aligned
            # with existing stream adapters.
            mentioned=True,
            raw_data=frame,
            timestamp=datetime.now(),
        )

    def _is_allowed(self, message: BotMessage) -> bool:
        if not self._admin_users:
            logger.warning(
                "[WeCom AI Bot] BOT_ADMIN_USERS 未配置，拒绝用户 %s 的消息；"
                "请将该 user_id 加入本机 .env 后重启服务。",
                message.user_id,
            )
            return False
        if message.user_id not in self._admin_users:
            logger.info("[WeCom AI Bot] 忽略非管理员用户 %s", message.user_id)
            return False
        if self._allowed_chat_ids and message.chat_id not in self._allowed_chat_ids:
            logger.info("[WeCom AI Bot] 忽略未授权会话 %s", message.chat_id)
            return False
        return True

    async def _reply_text(self, frame: Dict[str, Any], content: str) -> None:
        """Reply using the intelligent-bot stream message type.

        ``text`` and ``markdown`` are valid for welcome or proactive-message
        APIs, but normal message callbacks require a ``stream`` response.
        A completed one-shot stream works for both plain text and Markdown.
        """
        await self._client.reply_stream(
            frame,
            stream_id=f"dsa-{uuid.uuid4().hex}",
            content=content,
            finish=True,
        )

    async def _send_proactive_markdown(self, chat_id: str, content: str) -> None:
        """Send a message after the original callback window has expired."""
        await self._client.send_message(
            chat_id,
            {"markdown": {"content": content}},
        )

    async def _watch_analysis_task(
        self,
        task_id: str,
        chat_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 20 * 60,
    ) -> None:
        """Poll an async analysis and proactively return its final result."""
        from src.services.task_service import get_task_service

        service = get_task_service()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._running and loop.time() < deadline:
            task = service.get_task_status(task_id)
            if task:
                status = task.get("status")
                if status == "completed":
                    await self._send_proactive_markdown(
                        chat_id,
                        _format_analysis_followup(task),
                    )
                    logger.info("[WeCom AI Bot] 分析结果已回传: %s", task_id)
                    return
                if status == "failed":
                    error = _clean_report_text(task.get("error"), 240) or "未知错误"
                    await self._send_proactive_markdown(
                        chat_id,
                        f"❌ **分析失败**\n\n股票：`{task.get('code') or '--'}`\n\n原因：{error}",
                    )
                    return
            await asyncio.sleep(poll_interval)

        if self._running:
            await self._send_proactive_markdown(
                chat_id,
                "⏱️ **分析超时**\n\n任务未在20分钟内完成，请稍后重试。",
            )

    def _schedule_analysis_followup(self, task_id: str, chat_id: str) -> None:
        task = asyncio.create_task(self._watch_analysis_task(task_id, chat_id))
        self._followup_tasks.add(task)
        task.add_done_callback(self._followup_tasks.discard)

        def _log_failure(done: asyncio.Task[Any]) -> None:
            if not done.cancelled() and done.exception() is not None:
                logger.exception(
                    "[WeCom AI Bot] 分析结果回传失败: %s",
                    done.exception(),
                )

        task.add_done_callback(_log_failure)

    async def _handle_text(self, frame: Dict[str, Any]) -> None:
        message = self._frame_to_message(frame)
        if message is None or not self._is_allowed(message):
            return

        try:
            from bot.dispatcher import get_dispatcher

            response = await get_dispatcher().dispatch_async(message)
            if not response.text:
                return
            await self._reply_text(frame, response.text)
            task_id = str(response.extra.get("analysis_task_id") or "")
            if task_id:
                self._schedule_analysis_followup(task_id, message.chat_id)
        except Exception as exc:
            logger.exception("[WeCom AI Bot] 消息处理失败: %s", exc)
            try:
                await self._reply_text(frame, "暂时无法处理这条消息，请稍后再试。")
            except Exception:
                logger.exception("[WeCom AI Bot] 发送失败提示时发生异常")

    async def _run_session(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client = WSClient(
            bot_id=self._bot_id,
            secret=self._secret,
            max_reconnect_attempts=-1,
        )
        self._client.on("message.text", self._handle_text)
        await self._client.connect()
        logger.info("[WeCom AI Bot] 长连接已建立，等待消息。")
        while self._running:
            await asyncio.sleep(0.5)
        for task in tuple(self._followup_tasks):
            task.cancel()
        await self._client.disconnect()

    def _run_in_background(self) -> None:
        while self._running:
            try:
                asyncio.run(self._run_session())
            except Exception as exc:
                logger.error("[WeCom AI Bot] 长连接异常: %s", exc)
                if self._running:
                    threading.Event().wait(5)
            else:
                break

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("[WeCom AI Bot] 客户端已在运行")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_in_background,
            daemon=True,
            name="WeComAiBotClient",
        )
        self._thread.start()
        logger.info("[WeCom AI Bot] 后台客户端已启动")

    def stop(self) -> None:
        self._running = False
        if self._client and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._running)


_stream_client: Optional[WeComAiBotClient] = None


def get_wecom_aibot_client() -> Optional[WeComAiBotClient]:
    """Return the configured singleton, without starting it."""
    global _stream_client
    if _stream_client is None and WECOM_AIBOT_SDK_AVAILABLE:
        try:
            _stream_client = WeComAiBotClient()
        except (ImportError, ValueError) as exc:
            logger.warning("[WeCom AI Bot] 无法创建客户端: %s", exc)
            return None
    return _stream_client


def start_wecom_aibot_background() -> bool:
    """Start the configured intelligent bot without opening an HTTP port."""
    client = get_wecom_aibot_client()
    if not client:
        return False
    client.start_background()
    return True
