"""
Telegram Bot API alerts for Hammer live order events.

Uses stdlib urllib — no extra dependencies. Sends run in a background thread
so MT5 order flow is never blocked.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, FrozenSet, Optional, Tuple

ORDER_EVENTS: FrozenSet[str] = frozenset({
    "ORDER",
    "DRY_RUN",
    "ORDER_FAIL",
    "SAFETY_BLOCK",
    "EXIT",
})

EVENT_TITLES = {
    "ORDER": "ENTRY — ORDER PLACED",
    "DRY_RUN": "ENTRY SIGNAL — DRY RUN (not placed)",
    "ORDER_FAIL": "ENTRY FAILED",
    "SAFETY_BLOCK": "ENTRY BLOCKED (safety)",
    "EXIT": "EXIT — POSITION CLOSED",
}

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SEC = 2.0
DEFAULT_TIMEOUT_SEC = 15.0


def _fmt_price(value: float) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Gold-style 2dp; keep more digits for FX if needed
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.5f}"


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
    account_name: str = "",
    bot_name: str = "",
    risk: Optional[float] = None,
    rr_multiple: Optional[float] = None,
    magic: Optional[int] = None,
    pattern_variant: str = "",
    signal_bar_time: str = "",
    entry_bar_time: str = "",
    exit_price: Optional[float] = None,
    profit: Optional[float] = None,
    profit_currency: str = "",
    close_reason: str = "",
) -> str:
    """
    Client-facing trade card for Telegram.

    Includes strategy, entry/exit, SL/TP, RR, magic, account — enough to monitor
    without opening the dashboard.
    """
    is_dry = bool(dry_run) or event == "DRY_RUN"
    lane = "DRY RUN" if is_dry else "LIVE"
    title = EVENT_TITLES.get(event, event)
    is_exit = event == "EXIT"

    strategy = (pattern or "Strategy").strip()
    if pattern_variant:
        strategy = f"{strategy} ({pattern_variant})"

    risk_val = None
    try:
        if risk is not None and float(risk) > 0:
            risk_val = float(risk)
        elif entry_price and stop_loss:
            risk_val = abs(float(entry_price) - float(stop_loss))
    except (TypeError, ValueError):
        risk_val = None

    rr_val = None
    try:
        if rr_multiple is not None and float(rr_multiple) > 0:
            rr_val = float(rr_multiple)
        elif risk_val and risk_val > 0 and entry_price and target:
            rr_val = abs(float(target) - float(entry_price)) / risk_val
    except (TypeError, ValueError):
        rr_val = None

    lines = [
        f"Hammer [{lane}] — {title}",
        "————————————",
        f"Strategy: {strategy}",
        f"{symbol}  ·  {timeframe}  ·  {direction}  ·  {volume:g} lot",
    ]

    if is_exit:
        lines.append(f"Entry: {_fmt_price(entry_price)}")
        if exit_price is not None:
            lines.append(f"Exit:  {_fmt_price(exit_price)}")
        if profit is not None:
            cur = f" {profit_currency}" if profit_currency else ""
            sign = "+" if float(profit) >= 0 else ""
            lines.append(f"P/L:   {sign}{float(profit):.2f}{cur}")
        if close_reason:
            lines.append(f"Close: {close_reason}")
        lines.append(
            f"Plan was  SL {_fmt_price(stop_loss)}  ·  TP {_fmt_price(target)}"
        )
    else:
        if is_dry or event == "DRY_RUN":
            lines.append("No order sent — entry conditions only (no exit alert)")
        lines.append(f"Entry: {_fmt_price(entry_price)}")
        sl_bit = f"SL {_fmt_price(stop_loss)}"
        if risk_val is not None:
            sl_bit += f"  (risk {_fmt_price(risk_val)})"
        tp_bit = f"TP {_fmt_price(target)}"
        if rr_val is not None:
            tp_bit += f"  (RR 1:{rr_val:.2f})"
        lines.append(sl_bit)
        lines.append(tp_bit)

    meta_bits = []
    if order_mode and not is_exit and not is_dry:
        meta_bits.append(f"Mode {order_mode}")
    if magic is not None and int(magic) != 0:
        meta_bits.append(f"Magic {int(magic)}")
    if mt5_order_id and not is_dry:
        meta_bits.append(f"Ticket #{int(mt5_order_id)}")
    if meta_bits:
        lines.append(" · ".join(meta_bits))

    if account_name:
        lines.append(f"Account: {account_name}")
    if signal_bar_time:
        lines.append(f"Signal bar: {signal_bar_time}")
    if entry_bar_time and not is_exit:
        lines.append(f"Entry bar:  {entry_bar_time}")
    if bot_name:
        lines.append(f"Channel: {bot_name}")
    if mt5_message and event != "DRY_RUN":
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
    """Fire-and-forget Telegram alerts for a single bot/chat (legacy + per-bot send)."""

    def __init__(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
        log: Optional[LogFn] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_sec: float = DEFAULT_RETRY_DELAY_SEC,
        bot_name: str = "",
    ):
        self._token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._log = log or (lambda _msg: None)
        self._max_retries = max_retries
        self._retry_delay_sec = retry_delay_sec
        self._enabled_flag = bool(enabled)
        self._bot_name = (bot_name or "").strip()
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
        bot_name: Optional[str] = None,
    ) -> None:
        with self._lock:
            if enabled is not None:
                self._enabled_flag = bool(enabled)
            if bot_token is not None:
                self._token = bot_token.strip()
            if chat_id is not None:
                self._chat_id = chat_id.strip()
            if bot_name is not None:
                self._bot_name = bot_name.strip()
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
        account_name: str = "",
        account_id: str = "",  # accepted for NotificationManager API parity
        risk: Optional[float] = None,
        rr_multiple: Optional[float] = None,
        magic: Optional[int] = None,
        pattern_variant: str = "",
        signal_bar_time: str = "",
        entry_bar_time: str = "",
        exit_price: Optional[float] = None,
        profit: Optional[float] = None,
        profit_currency: str = "",
        close_reason: str = "",
    ) -> None:
        with self._lock:
            enabled = self.enabled
            bot_name = self._bot_name
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
            account_name=account_name or account_id,
            bot_name=bot_name,
            risk=risk,
            rr_multiple=rr_multiple,
            magic=magic,
            pattern_variant=pattern_variant,
            signal_bar_time=signal_bar_time,
            entry_bar_time=entry_bar_time,
            exit_price=exit_price,
            profit=profit,
            profit_currency=profit_currency,
            close_reason=close_reason,
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
            bot_name = self._bot_name
        ok, detail = send_message(
            token,
            chat_id,
            text,
            max_retries=max_retries,
            retry_delay_sec=retry_delay,
        )
        label = f"/{bot_name}" if bot_name else ""
        if ok:
            self._log(f"[TELEGRAM{label}] Alert sent ({event}).")
        else:
            self._log(f"[TELEGRAM{label}] Alert failed after retries ({event}): {detail}")
