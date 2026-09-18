"""
Notification package — Telegram alerts for Hammer live / dry-run trades.

Supports multiple bots with Live vs Dry-run routing so clients can tell
real money alerts from simulation alerts.
"""

from .telegram import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY_SEC,
    DEFAULT_TIMEOUT_SEC,
    EVENT_TITLES,
    ORDER_EVENTS,
    TelegramNotifier,
    format_order_alert,
    send_message,
)
from .manager import (
    MODE_BOTH,
    MODE_DRY_RUN,
    MODE_LIVE,
    NotificationBot,
    NotificationEvent,
    NotificationManager,
)
from .store import default_bots_path, load_bots, save_bots
from .workers import TelegramTestWorker

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_DELAY_SEC",
    "DEFAULT_TIMEOUT_SEC",
    "EVENT_TITLES",
    "MODE_BOTH",
    "MODE_DRY_RUN",
    "MODE_LIVE",
    "ORDER_EVENTS",
    "NotificationBot",
    "NotificationEvent",
    "NotificationManager",
    "TelegramNotifier",
    "TelegramTestWorker",
    "default_bots_path",
    "format_order_alert",
    "load_bots",
    "save_bots",
    "send_message",
]
