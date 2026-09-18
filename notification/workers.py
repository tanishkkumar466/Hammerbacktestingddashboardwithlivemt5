"""Background workers for Telegram — keep network off the GUI thread."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from . import telegram as telegram_notify


class TelegramTestWorker(QThread):
    """POST a test message without blocking the dashboard."""

    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, bot_token: str, chat_id: str, text: str, parent=None):
        super().__init__(parent)
        self._token = bot_token
        self._chat_id = chat_id
        self._text = text

    def run(self):
        try:
            ok, detail = telegram_notify.send_message(
                self._token,
                self._chat_id,
                self._text,
                max_retries=telegram_notify.DEFAULT_MAX_RETRIES,
            )
            if ok:
                self.finished_ok.emit(detail or "sent")
            else:
                self.failed.emit(detail or "unknown error")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
