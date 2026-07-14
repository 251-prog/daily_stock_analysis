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
