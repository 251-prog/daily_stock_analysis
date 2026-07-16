# -*- coding: utf-8 -*-
"""Recent company announcements command."""

from __future__ import annotations

from typing import List, Optional

from bot.commands.base import BotCommand
from bot.commands.quote import is_supported_stock_code
from bot.models import BotMessage, BotResponse
from data_provider.base import normalize_stock_code
from src.services.announcement_service import AnnouncementService


class AnnouncementsCommand(BotCommand):
    @property
    def name(self) -> str:
        return "announcements"

    @property
    def aliases(self) -> List[str]:
        return ["notice", "公告", "新闻", "消息", "业绩"]

    @property
    def description(self) -> str:
        return "查询近期公司公告（不调用 AI）"

    @property
    def usage(self) -> str:
        return "/announcements <股票代码>"

    @property
    def admin_only(self) -> bool:
        return True

    def validate_args(self, args: List[str]) -> Optional[str]:
        if not args:
            return "请提供股票代码"
        if not is_supported_stock_code(args[0]):
            return "股票代码格式不正确（例如：603629）"
        return None

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        code = normalize_stock_code(args[0])
        if not (code.isdigit() and len(code) == 6):
            return BotResponse.text_response("目前公告直连查询先支持 A 股。")

        try:
            items = AnnouncementService().get_recent(code, days=3, limit=5)
        except Exception:
            return BotResponse.error_response(
                f"暂时无法读取 `{code}` 的公开公告，请稍后再试。"
            )

        if not items:
            return BotResponse.markdown_response(
                f"📋 `{code}` **近3日未查到公司公告**\n\n"
                "这只代表当前公告接口没有返回结果，不代表可以确认“没有重大事件”。"
            )

        first = items[0]
        lines = [
            f"📋 **{code} 近3日公司公告**",
            "",
            f"最新：**{first['title']}**",
            f"接口收录日期：{str(first.get('display_time') or first.get('notice_date'))[:10]}",
        ]
        if first.get("summary"):
            lines.extend(["", "**公告要点**", str(first["summary"])])
        lines.extend(["", f"[查看公告原文]({first['url']})"])

        if len(items) > 1:
            lines.extend(["", "**其他近期公告**"])
            for item in items[1:4]:
                date_text = str(item.get("display_time") or item.get("notice_date"))[:10]
                lines.append(f"- {date_text} [{item['title']}]({item['url']})")

        lines.extend(
            [
                "",
                "_本查询直连公司公告数据，未调用 DeepSeek。"
                "具体数据以交易所披露原文为准。_",
            ]
        )
        return BotResponse.markdown_response("\n".join(lines))
