# -*- coding: utf-8 -*-
"""Owner-only watchlist management for conversational bots."""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from bot.commands.base import BotCommand
from bot.commands.quote import is_supported_stock_code
from bot.models import BotMessage, BotResponse
from data_provider.base import normalize_stock_code
from src.services.stock_list_parser import split_stock_list
from src.services.system_config_service import SystemConfigService


class WatchlistCommand(BotCommand):
    """Manage the local ``STOCK_LIST`` after an explicit confirmation."""

    _PENDING_TTL_SECONDS = 15 * 60
    _pending_additions: Dict[str, Tuple[str, float]] = {}
    _pending_lock = threading.Lock()

    def __init__(
        self,
        service_factory: Callable[[], SystemConfigService] = SystemConfigService,
    ) -> None:
        self._service_factory = service_factory

    @property
    def name(self) -> str:
        return "watchlist"

    @property
    def aliases(self) -> List[str]:
        return ["wl", "自选", "常用股"]

    @property
    def description(self) -> str:
        return "管理常用股（日报与重要提醒范围）"

    @property
    def usage(self) -> str:
        return "/watchlist [list|add|confirm|remove] [股票代码]"

    @property
    def admin_only(self) -> bool:
        return True

    @staticmethod
    def _match_key(code: str) -> str:
        normalized = normalize_stock_code(code.strip())
        if normalized.isdigit() and len(normalized) == 5:
            return f"HK{normalized}"
        return normalized.upper()

    @staticmethod
    def _read_codes(service: SystemConfigService) -> Tuple[List[str], str]:
        config_data = service.get_config(include_schema=False)
        value = ""
        for item in config_data.get("items", []):
            if item.get("key") == "STOCK_LIST":
                value = str(item.get("value", ""))
                break
        return split_stock_list(value), str(config_data.get("config_version", ""))

    @staticmethod
    def _write_codes(service: SystemConfigService, codes: List[str], config_version: str) -> None:
        service.update(
            config_version=config_version,
            items=[{"key": "STOCK_LIST", "value": ",".join(codes)}],
            mask_token="******",
            reload_now=True,
        )

    @classmethod
    def _set_pending(cls, user_id: str, code: str) -> None:
        with cls._pending_lock:
            cls._pending_additions[user_id] = (code, time.monotonic() + cls._PENDING_TTL_SECONDS)

    @classmethod
    def _pop_pending(cls, user_id: str, code: str) -> bool:
        with cls._pending_lock:
            pending = cls._pending_additions.pop(user_id, None)
        return bool(
            pending
            and pending[1] >= time.monotonic()
            and cls._match_key(pending[0]) == cls._match_key(code)
        )

    def validate_args(self, args: List[str]) -> Optional[str]:
        if not args:
            return None
        action = args[0].lower()
        if action in {"list", "列表", "查看"} and len(args) == 1:
            return None
        if action in {"add", "添加", "confirm", "确认", "remove", "删除", "移除"}:
            if len(args) != 2:
                return "请提供股票代码"
            if not is_supported_stock_code(args[1]):
                return "股票代码格式不正确（例如：600519、hk00700、AAPL）"
            return None
        return "支持 list、add、confirm、remove 四个操作"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        action = args[0].lower() if args else "list"
        if action in {"list", "列表", "查看"}:
            service = self._service_factory()
            codes, _ = self._read_codes(service)
            if not codes:
                return BotResponse.text_response("当前没有常用股。")
            return BotResponse.markdown_response(
                "📌 **常用股**（进入盘后日报与重要事件提醒）\n\n"
                + "、".join(f"`{code}`" for code in codes)
                + "\n\n添加：`自选 add 600519`\n移除：`自选 remove 600519`"
            )

        code = args[1].strip()
        if action in {"add", "添加"}:
            self._set_pending(message.user_id, code)
            return BotResponse.markdown_response(
                f"已准备把 `{code.upper()}` 加入常用股。\n\n"
                f"确认后，它才会进入盘后日报和重要事件提醒。\n"
                f"请发送：`自选 confirm {code.upper()}`"
            )

        if action in {"confirm", "确认"}:
            if not self._pop_pending(message.user_id, code):
                return BotResponse.error_response(
                    f"没有找到 `{code.upper()}` 的待确认操作。请先发送：`自选 add {code.upper()}`"
                )
            service = self._service_factory()
            codes, config_version = self._read_codes(service)
            if self._match_key(code) in {self._match_key(item) for item in codes}:
                return BotResponse.text_response(f"`{code.upper()}` 已经是常用股。")
            codes.append(code.upper())
            self._write_codes(service, codes, config_version)
            return BotResponse.markdown_response(
                f"✅ 已将 `{code.upper()}` 加入常用股。\n"
                "它会从下一次本机定时任务开始进入盘后日报与重要事件提醒。"
            )

        if action in {"remove", "删除", "移除"}:
            service = self._service_factory()
            codes, config_version = self._read_codes(service)
            key = self._match_key(code)
            updated = [item for item in codes if self._match_key(item) != key]
            if len(updated) == len(codes):
                return BotResponse.text_response(f"`{code.upper()}` 不在当前常用股中。")
            self._write_codes(service, updated, config_version)
            return BotResponse.markdown_response(
                f"已将 `{code.upper()}` 移出常用股。\n"
                "它不再进入后续盘后日报与重要事件提醒。"
            )

        return BotResponse.error_response("不支持的常用股操作")
