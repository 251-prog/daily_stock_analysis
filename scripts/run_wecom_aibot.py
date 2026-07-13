#!/usr/bin/env python3
"""Run only the Enterprise WeCom intelligent bot long connection.

This intentionally does not execute the daily-report pipeline.  It lets a
personal always-on computer answer group messages while GitHub Actions remains
the single source of scheduled reports and outbound group notifications.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_config, setup_env
from src.logging_config import setup_logging


def main() -> int:
    setup_env()
    setup_logging()
    config = get_config()
    if not getattr(config, "wecom_aibot_enabled", False):
        logging.error("WECOM_AIBOT_ENABLED 未启用；请先在本机 .env 设置为 true。")
        return 2

    from bot.platforms.wecom_aibot import get_wecom_aibot_client, start_wecom_aibot_background

    if not start_wecom_aibot_background():
        logging.error("智能机器人无法启动；请检查 ID、Secret 和依赖。")
        return 2

    client = get_wecom_aibot_client()
    stopping = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if client:
            client.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logging.info("企业微信智能机器人正在运行；按 Ctrl+C 停止。")
    try:
        while not stopping:
            time.sleep(1)
    finally:
        if client:
            client.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
