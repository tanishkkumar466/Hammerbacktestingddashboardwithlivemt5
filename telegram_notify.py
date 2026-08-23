"""
Telegram Bot API alerts for Hammer live order events.

Uses stdlib urllib — no extra dependencies. Sends run in a background thread
from the live worker so MT5 order flow is never blocked.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, FrozenSet, Optional, Tuple

ORDER_EVENTS: FrozenSet[str] = frozenset({"ORDER", "DRY_RUN", "ORDER_FAIL", "SAFETY_BLOCK"})

EVENT_TITLES = {
    "ORDER": "ORDER PLACED",
    "DRY_RUN": "DRY RUN (simulated order)",
    "ORDER_FAIL": "ORDER FAILED",
    "SAFETY_BLOCK": "ORDER BLOCKED (safety)",
}

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SEC = 2.0
DEFAULT_TIMEOUT_SEC = 15.0


def format_order_alert(
    event: str,
    *,
    symbol: str,
    timeframe: str,
    pattern: str,
    direction: str,
    volume: float,
    entry_price: float,
    stop_loss: float,
    target: float,
    dry_run: bool = False,
    order_mode: str = "",
    mt5_order_id: Optional[int] = None,
    mt5_message: str = "",
) -> str:
    title = EVENT_TITLES.get(event, event)
    lines = [
        f"Hammer Live — {title}",
        f"{symbol} | {direction} | {volume:g} lot(s) | {timeframe}",
        f"Entry ~{entry_price:.2f} | SL {stop_loss:.2f} | TP {target:.2f}",
    ]
    if pattern:
        lines.append(f"Pattern: {pattern}")
    if order_mode:
        lines.append(f"Order mode: {order_mode}")
    if mt5_order_id:
        lines.append(f"MT5 order #{mt5_order_id}")
    if dry_run and event != "DRY_RUN":
        lines.append("Dry run: yes")
    if mt5_message:
        lines.append(f"Detail: {mt5_message}")
    return "\n".join(lines)


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_delay_sec: float = DEFAULT_RETRY_DELAY_SEC,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> Tuple[bool, str]:
    """
    POST to Telegram sendMessage with retries. Returns (success, detail).
    """
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()
    if not token:
        return False, "Bot token is empty"
    if not cid:
        return False, "Chat ID is empty"
    if not (text or "").strip():
        return False, "Message text is empty"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": cid,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    attempts = max(1, int(max_retries))
    last_err = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            if payload.get("ok"):
                return True, "sent"
            last_err = payload.get("description") or raw
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
                payload = json.loads(err_body)
                last_err = payload.get("description") or err_body
            except Exception:
                last_err = str(e)
        except urllib.error.URLError as e:
            last_err = str(e.reason if hasattr(e, "reason") else e)
        except TimeoutError:
            last_err = "request timed out"
        except json.JSONDecodeError as e:
            last_err = f"invalid JSON response: {e}"
        except Exception as e:
            last_err = str(e)

        if attempt < attempts:
            time.sleep(retry_delay_sec)

    return False, last_err


LogFn = Callable[[str], None]


class TelegramNotifier:
    """Fire-and-forget Telegram alerts for live order events."""

    def __init__(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
        log: Optional[LogFn] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_sec: float = DEFAULT_RETRY_DELAY_SEC,
    ):
        self._token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._log = log or (lambda _msg: None)
        self._max_retries = max_retries
        self._retry_delay_sec = retry_delay_sec
        self._enabled_flag = bool(enabled)
        self._lock = threading.Lock()
        self.enabled = self._enabled_flag and bool(self._token) and bool(self._chat_id)

    @classmethod
    def from_live_config(cls, cfg, log: Optional[LogFn] = None) -> "TelegramNotifier":
        return cls(
            enabled=bool(getattr(cfg, "telegram_enabled", False)),
            bot_token=str(getattr(cfg, "telegram_bot_token", "") or ""),
            chat_id=str(getattr(cfg, "telegram_chat_id", "") or ""),
            log=log,
        )

    def update(
        self,
        *,
        enabled: Optional[bool] = None,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            if enabled is not None:
                self._enabled_flag = bool(enabled)
            if bot_token is not None:
                self._token = bot_token.strip()
            if chat_id is not None:
                self._chat_id = chat_id.strip()
            self.enabled = self._enabled_flag and bool(self._token) and bool(self._chat_id)

    def notify_order_event(
        self,
        event: str,
        *,
        symbol: str,
        timeframe: str,
        pattern: str,
        direction: str,
        volume: float,
        entry_price: float,
        stop_loss: float,
        target: float,
        dry_run: bool = False,
        order_mode: str = "",
        mt5_order_id: Optional[int] = None,
        mt5_message: str = "",
    ) -> None:
        with self._lock:
            enabled = self.enabled
        if not enabled or event not in ORDER_EVENTS:
            return
        text = format_order_alert(
            event,
            symbol=symbol,
            timeframe=timeframe,
            pattern=pattern,
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target,
            dry_run=dry_run,
            order_mode=order_mode,
            mt5_order_id=mt5_order_id,
            mt5_message=mt5_message,
        )
        threading.Thread(
            target=self._send_and_log,
            args=(event, text),
            daemon=True,
            name="telegram-notify",
        ).start()

    def _send_and_log(self, event: str, text: str) -> None:
        with self._lock:
            token = self._token
            chat_id = self._chat_id
            max_retries = self._max_retries
            retry_delay = self._retry_delay_sec
        ok, detail = send_message(
            token,
            chat_id,
            text,
            max_retries=max_retries,
            retry_delay_sec=retry_delay,
        )
        if ok:
            self._log(f"[TELEGRAM] Alert sent ({event}).")
        else:
            self._log(f"[TELEGRAM] Alert failed after retries ({event}): {detail}")
