"""Compatibility shim — Telegram workers live in notification.workers."""

from notification.workers import TelegramTestWorker  # noqa: F401

__all__ = ["TelegramTestWorker"]
