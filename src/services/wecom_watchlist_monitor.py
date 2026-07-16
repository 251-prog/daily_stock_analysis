# -*- coding: utf-8 -*-
"""Low-noise watchlist alerts for the Enterprise WeCom intelligent bot."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src.core.trading_calendar import is_market_open
from src.services.announcement_service import AnnouncementService
from src.services.stock_service import StockService
from src.stock_analyzer import MACDStatus, StockTrendAnalyzer, TrendStatus

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
        self.closing_brief_enabled = bool(
            getattr(config, "wecom_closing_brief_enabled", False)
        )
        self.closing_brief_hour, self.closing_brief_minute = self._parse_hhmm(
            str(getattr(config, "wecom_closing_brief_time", "14:30"))
        )
        self.chat_ids = list(getattr(config, "wecom_aibot_allowed_chat_ids", []) or [])
        self.state_path = state_path or Path("data/wecom_watchlist_monitor_state.json")
        self.now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))
        self.stock_service = stock_service or StockService()
        self.announcement_service = announcement_service or AnnouncementService()
        self.trend_analyzer = StockTrendAnalyzer()
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
            "closing_brief_last_sent": "",
        }

    @staticmethod
    def _parse_hhmm(raw: str) -> Tuple[int, int]:
        try:
            hour_text, minute_text = raw.strip().split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("time out of range")
            return hour, minute
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "[WeCom Monitor] WECOM_CLOSING_BRIEF_TIME=%r 无效，回退到14:30",
                raw,
            )
            return 14, 30

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

    def _send(self, content: str) -> bool:
        sent_any = False
        for chat_id in self.chat_ids:
            try:
                if self.sender(chat_id, content):
                    sent_any = True
                else:
                    logger.warning("[WeCom Monitor] 提醒发送失败，会话=%s", chat_id)
            except Exception as exc:
                logger.warning("[WeCom Monitor] 提醒发送异常: %s", exc)
        return sent_any

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

    def _closing_brief_due(self, now: datetime) -> bool:
        if not self.closing_brief_enabled or now.weekday() >= 5:
            return False
        if not is_market_open("cn", now.date()):
            return False
        minute = now.hour * 60 + now.minute
        scheduled = self.closing_brief_hour * 60 + self.closing_brief_minute
        if minute < scheduled or minute >= 15 * 60:
            return False
        return self._state.get("closing_brief_last_sent") != now.date().isoformat()

    @staticmethod
    def _positive_number(value: Any, fallback: float = 0.0) -> float:
        try:
            number = float(value)
            return number if number > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    def _build_intraday_frame(
        self,
        code: str,
        quote: Dict[str, Any],
        now: datetime,
    ) -> pd.DataFrame:
        history = self.stock_service.get_history_data(code, days=80).get("data") or []
        rows = [dict(item) for item in history if item.get("date") and item.get("close")]
        if not rows:
            return pd.DataFrame()

        rows.sort(key=lambda item: str(item.get("date")))
        today = now.date().isoformat()
        existing = rows[-1] if str(rows[-1].get("date"))[:10] == today else {}
        previous_rows = rows[:-1] if existing else rows
        previous_close = self._positive_number(
            quote.get("prev_close"),
            self._positive_number(previous_rows[-1].get("close") if previous_rows else None),
        )
        current_price = self._positive_number(quote.get("current_price"), previous_close)
        open_price = self._positive_number(
            quote.get("open"),
            self._positive_number(existing.get("open"), previous_close or current_price),
        )
        high = max(
            current_price,
            self._positive_number(quote.get("high")),
            self._positive_number(existing.get("high")),
        )
        low_candidates = [
            value
            for value in (
                current_price,
                self._positive_number(quote.get("low")),
                self._positive_number(existing.get("low")),
            )
            if value > 0
        ]
        low = min(low_candidates) if low_candidates else current_price
        recent_volumes = [
            self._positive_number(item.get("volume")) for item in previous_rows[-5:]
        ]
        recent_volumes = [value for value in recent_volumes if value > 0]
        fallback_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0.0
        volume = self._positive_number(
            quote.get("volume"),
            self._positive_number(existing.get("volume"), fallback_volume),
        )

        intraday_row = {
            "date": today,
            "open": open_price,
            "high": high,
            "low": low,
            "close": current_price,
            "volume": volume,
            "amount": quote.get("amount") or existing.get("amount"),
        }
        if existing:
            rows[-1] = intraday_row
        else:
            rows.append(intraday_row)
        return pd.DataFrame(rows)

    @staticmethod
    def _closing_plan(result: Any, change_pct: float) -> Dict[str, str]:
        bearish = result.trend_status in {
            TrendStatus.WEAK_BEAR,
            TrendStatus.BEAR,
            TrendStatus.STRONG_BEAR,
        }
        bullish = result.trend_status in {TrendStatus.BULL, TrendStatus.STRONG_BULL}
        below_ma20 = result.ma20 > 0 and result.current_price < result.ma20
        overheated = (
            result.bias_ma5 > 5
            or result.rsi_12 >= 70
            or (result.kdj_k >= 80 and result.kdj_d >= 80)
        )
        momentum_positive = result.macd_status not in {
            MACDStatus.DEATH_CROSS,
            MACDStatus.CROSSING_DOWN,
            MACDStatus.BEARISH,
        }

        if change_pct <= -5 or (bearish and below_ma20):
            level = result.ma20 or result.ma10
            return {
                "label": "防守优先",
                "holding": (
                    f"关注 {level:.2f} 附近；若14:45后仍无法收回且反弹量能不足，"
                    "优先降低风险，不在尾盘急补仓。"
                ),
                "empty": "继续等待，不做尾盘抄底；先看收盘能否重新站回MA20。",
            }
        if bullish and momentum_positive and not overheated:
            return {
                "label": "偏强观察",
                "holding": (
                    f"以MA5 {result.ma5:.2f}为尾盘观察线；守住可继续观察，"
                    "跌回其下则降低次日预期。"
                ),
                "empty": "不追最后30分钟拉升；等待收盘确认多头结构，次日再评估回踩机会。",
            }
        if overheated:
            return {
                "label": "强势但过热",
                "holding": "防止冲高回落；尾盘若放量滞涨，可考虑先收缩风险敞口。",
                "empty": "不追高，等待乖离率和KDJ回落后再观察。",
            }
        key_level = result.ma20 if result.ma20 > 0 else result.ma10
        return {
            "label": "震荡等待",
            "holding": f"以 {key_level:.2f} 为强弱分界，尾盘不因单次脉冲频繁操作。",
            "empty": "等待收盘方向确认；没有趋势共振时保持空仓观察。",
        }

    def _analyze_closing_stock(
        self,
        code: str,
        now: datetime,
    ) -> Optional[Dict[str, Any]]:
        quote = self.stock_service.get_realtime_quote(code)
        if not quote or self._positive_number(quote.get("current_price")) <= 0:
            return None
        frame = self._build_intraday_frame(code, quote, now)
        if frame.empty or len(frame) < 26:
            return None
        result = self.trend_analyzer.analyze(frame, code)
        change_pct = float(quote.get("change_percent") or 0.0)
        return {
            "code": code,
            "name": str(quote.get("stock_name") or code),
            "price": self._positive_number(quote.get("current_price")),
            "change_pct": change_pct,
            "high": self._positive_number(quote.get("high")),
            "low": self._positive_number(quote.get("low")),
            "result": result,
            "plan": self._closing_plan(result, change_pct),
        }

    @staticmethod
    def _format_closing_brief(
        items: List[Dict[str, Any]],
        failed_codes: List[str],
        now: datetime,
    ) -> str:
        up_count = sum(1 for item in items if item["change_pct"] > 0)
        down_count = sum(1 for item in items if item["change_pct"] < 0)
        lines = [
            f"## 🕝 尾盘30分钟简报｜{now.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"> 自选股 {len(items)} 只｜上涨 {up_count}｜下跌 {down_count}",
            "",
            "以下使用14:30附近未收盘实时数据，重点是收盘确认与风险控制。",
        ]
        for item in items:
            result = item["result"]
            plan = item["plan"]
            range_text = "--"
            if item["high"] > 0 and item["low"] > 0:
                range_text = f"{item['low']:.2f}～{item['high']:.2f}"
            lines.extend(
                [
                    "",
                    f"### {item['name']}（{item['code']}）｜{plan['label']}",
                    f"现价 **{item['price']:.2f}**｜涨跌 **{item['change_pct']:+.2f}%**｜日内 {range_text}",
                    f"趋势：{result.ma_alignment}｜MA5 {result.ma5:.2f} / MA20 {result.ma20:.2f}",
                    f"动能：RSI12 {result.rsi_12:.1f}｜KDJ {result.kdj_k:.1f}/{result.kdj_d:.1f}/{result.kdj_j:.1f}｜{result.macd_signal}",
                    f"**持仓者：** {plan['holding']}",
                    f"**空仓者：** {plan['empty']}",
                ]
            )
        if failed_codes:
            lines.extend(
                [
                    "",
                    f"⚠️ 数据不足，暂未判断：{', '.join(failed_codes)}",
                ]
            )
        lines.extend(
            [
                "",
                "_本简报不调用模型、不自动下单；盘中指标基于未完成日线，仅供个人研究。_",
            ]
        )
        return "\n".join(lines)

    def _send_closing_brief(self, stocks: List[str], now: datetime) -> None:
        items: List[Dict[str, Any]] = []
        failed_codes: List[str] = []
        for code in stocks:
            if not self._is_a_share(code):
                continue
            try:
                item = self._analyze_closing_stock(code, now)
            except Exception as exc:
                logger.warning("[WeCom Monitor] %s 尾盘分析失败: %s", code, exc)
                item = None
            if item:
                items.append(item)
            else:
                failed_codes.append(code)
        if not items:
            logger.warning("[WeCom Monitor] 尾盘简报无可用行情，将在15:00前重试")
            return
        content = self._format_closing_brief(items, failed_codes, now)
        if self._send(content):
            self._state["closing_brief_last_sent"] = now.date().isoformat()
            logger.info(
                "[WeCom Monitor] 尾盘简报发送成功：%d只成功，%d只降级",
                len(items),
                len(failed_codes),
            )

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
        if self._closing_brief_due(now):
            self._send_closing_brief(stocks, now)
        self._save_state()

    def _run(self) -> None:
        logger.info(
            "[WeCom Monitor] 已启动：%d只自选股，间隔%d分钟，涨跌阈值%.1f%%，尾盘简报=%s %02d:%02d",
            len(self.config.stock_list),
            self.interval_seconds // 60,
            self.move_alert_pct,
            "开启" if self.closing_brief_enabled else "关闭",
            self.closing_brief_hour,
            self.closing_brief_minute,
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
