"""Compatibility shim — Telegram notify lives in notification.telegram."""

from notification.telegram import *  # noqa: F401,F403
from notification.telegram import (  # noqa: F401
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY_SEC,
    DEFAULT_TIMEOUT_SEC,
    EVENT_TITLES,
    ORDER_EVENTS,
    TelegramNotifier,
    format_order_alert,
    send_message,
)
