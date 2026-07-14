# -*- coding: utf-8 -*-
"""Low-noise watchlist alerts for the Enterprise WeCom intelligent bot."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.services.announcement_service import AnnouncementService
from src.services.stock_service import StockService

logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_IMPORTANT_ANNOUNCEMENT_KEYWORDS = (
    "业绩",
    "预告",
    "快报",
    "重大",
    "停牌",
    "复牌",
    "回购",
    "增持",
    "减持",
    "并购",
    "重组",
    "中标",
    "合同",
    "风险提示",
    "诉讼",
    "处罚",
    "控制权",
    "分红",
)


class WeComWatchlistMonitor:
    """Poll deterministic market facts and send deduplicated important alerts."""

    def __init__(
        self,
        config: Any,
        sender: Callable[[str, str], bool],
        *,
        state_path: Optional[Path] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        stock_service: Optional[StockService] = None,
        announcement_service: Optional[AnnouncementService] = None,
    ) -> None:
        self.config = config
        self.sender = sender
        self.interval_seconds = max(
            60,
            int(getattr(config, "wecom_watchlist_monitor_interval_minutes", 5)) * 60,
        )
        self.move_alert_pct = max(
            1.0, float(getattr(config, "wecom_watchlist_move_alert_pct", 5.0))
        )
        self.chat_ids = list(getattr(config, "wecom_aibot_allowed_chat_ids", []) or [])
        self.state_path = state_path or Path("data/wecom_watchlist_monitor_state.json")
        self.now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self.stock_service = stock_service or StockService()
        self.announcement_service = announcement_service or AnnouncementService()
        self._state = self._load_state()
        self._levels_cache: Dict[str, tuple[str, Dict[str, float]]] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _load_state(self) -> Dict[str, Any]:
        try:
            if self.state_path.is_file():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("[WeCom Monitor] 状态读取失败，将重新初始化: %s", exc)
        return {
            "seen_announcements": {},
            "move_alerts": {},
            "level_sides": {},
        }

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.state_path)
        except OSError as exc:
            logger.warning("[WeCom Monitor] 状态保存失败: %s", exc)

    @staticmethod
    def _is_a_share(code: str) -> bool:
        return code.isdigit() and len(code) == 6

    @staticmethod
    def _is_trading_time(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        minute = now.hour * 60 + now.minute
        return (9 * 60 + 25 <= minute <= 11 * 60 + 35) or (
            13 * 60 <= minute <= 15 * 60 + 5
        )

    def _send(self, content: str) -> None:
        for chat_id in self.chat_ids:
            try:
                if not self.sender(chat_id, content):
                    logger.warning("[WeCom Monitor] 提醒发送失败，会话=%s", chat_id)
            except Exception as exc:
                logger.warning("[WeCom Monitor] 提醒发送异常: %s", exc)

    def _check_announcements(self, code: str) -> None:
        if not self._is_a_share(code):
            return
        items = self.announcement_service.get_recent(
            code, days=3, limit=12, include_content=True
        )
        seen_map = self._state.setdefault("seen_announcements", {})
        existing = set(seen_map.get(code, []))
        current_ids = [str(item.get("art_code") or item.get("url") or "") for item in items]
        current_ids = [item_id for item_id in current_ids if item_id]

        # First run establishes a baseline and never floods the group with old filings.
        if code not in seen_map:
            seen_map[code] = current_ids[:50]
            return

        important_new = []
        for item in items:
            item_id = str(item.get("art_code") or item.get("url") or "")
            title = str(item.get("title") or "")
            if item_id and item_id not in existing and any(
                keyword in title for keyword in _IMPORTANT_ANNOUNCEMENT_KEYWORDS
            ):
                important_new.append(item)

        if important_new:
            for item in reversed(important_new[:3]):
                date_text = str(item.get("display_time") or item.get("notice_date") or "")[:16]
                summary = str(item.get("summary") or "").strip()
                lines = [
                    f"🔔 **自选股重要公告｜{code}**",
                    "",
                    f"**{item.get('title') or '未命名公告'}**",
                    f"披露时间：{date_text or '待确认'}",
                ]
                if summary:
                    lines.extend(["", summary[:650]])
                if item.get("url"):
                    lines.extend(["", f"[查看公告原文]({item['url']})"])
                lines.extend(["", "_确定性规则触发，未调用模型；请以交易所原文为准。_"])
                self._send("\n".join(lines))

        merged = list(dict.fromkeys(current_ids + list(existing)))[:50]
        seen_map[code] = merged

    def _get_levels(self, code: str, today: str) -> Dict[str, float]:
        cached = self._levels_cache.get(code)
        if cached and cached[0] == today:
            return cached[1]
        history = self.stock_service.get_history_data(code, days=35).get("data") or []
        closes = [float(item["close"]) for item in history if item.get("close")]
        levels: Dict[str, float] = {}
        if len(closes) >= 20:
            prior = closes[-20:]
            levels = {
                "MA20": sum(prior) / len(prior),
                "20日高点": max(prior),
                "20日低点": min(prior),
            }
        self._levels_cache[code] = (today, levels)
        return levels

    def _check_market_move(self, code: str, now: datetime) -> None:
        if not self._is_a_share(code):
            return
        quote = self.stock_service.get_realtime_quote(code)
        if not quote:
            return
        price = float(quote.get("current_price") or 0)
        change_pct = float(quote.get("change_percent") or 0)
        if price <= 0:
            return

        today = now.date().isoformat()
        name = str(quote.get("stock_name") or code)
        alerts: List[str] = []
        move_key = f"{today}:{'up' if change_pct >= 0 else 'down'}"
        move_alerts = self._state.setdefault("move_alerts", {})
        if abs(change_pct) >= self.move_alert_pct and move_alerts.get(code) != move_key:
            alerts.append(f"日内涨跌幅达到 **{change_pct:+.2f}%**")
            move_alerts[code] = move_key

        levels = self._get_levels(code, today)
        side_state = self._state.setdefault("level_sides", {}).setdefault(code, {})
        for label, level in levels.items():
            side = "above" if price >= level else "below"
            previous = side_state.get(label)
            if previous and previous != side:
                verb = "上穿" if side == "above" else "下破"
                alerts.append(f"现价 **{price:.2f}** {verb}{label} **{level:.2f}**")
            side_state[label] = side

        if alerts:
            lines = [
                f"⚡ **盘中重要提醒｜{name}（{code}）**",
                "",
                *[f"- {item}" for item in alerts],
                "",
                f"当前价：**{price:.2f}** ｜ 涨跌：**{change_pct:+.2f}%**",
                "",
                "_仅在异常波动或关键技术位跨越时提醒；未调用模型，不构成交易建议。_",
            ]
            self._send("\n".join(lines))

    def run_once(self) -> None:
        stocks = [str(code).strip().upper() for code in self.config.stock_list if str(code).strip()]
        now = self.now_fn()
        for code in stocks:
            try:
                self._check_announcements(code)
            except Exception as exc:
                logger.warning("[WeCom Monitor] %s 公告检查失败: %s", code, exc)
            if self._is_trading_time(now):
                try:
                    self._check_market_move(code, now)
                except Exception as exc:
                    logger.warning("[WeCom Monitor] %s 盘中检查失败: %s", code, exc)
        self._save_state()

    def _run(self) -> None:
        logger.info(
            "[WeCom Monitor] 已启动：%d只自选股，间隔%d分钟，涨跌阈值%.1f%%",
            len(self.config.stock_list),
            self.interval_seconds // 60,
            self.move_alert_pct,
        )
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.interval_seconds)

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="WeComWatchlistMonitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

