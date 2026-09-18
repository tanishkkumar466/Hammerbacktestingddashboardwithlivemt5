"""
Multi-bot notification manager.

Routes order events to one or more Telegram bots based on:
  - Live vs Dry-run mode
  - Optional account id filter
  - Optional timeframe filter

Keeps an in-memory event list for the dashboard Notification Manager UI.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from .telegram import ORDER_EVENTS, TelegramNotifier, format_order_alert

LogFn = Callable[[str], None]

MODE_LIVE = "live"
MODE_DRY_RUN = "dry_run"
MODE_BOTH = "both"


@dataclass
class NotificationBot:
    id: str
    name: str
    bot_token: str
    chat_id: str
    enabled: bool = True
    mode: str = MODE_BOTH  # live | dry_run | both
    account_ids: List[str] = field(default_factory=list)  # empty = all accounts
    timeframes: List[str] = field(default_factory=list)  # empty = all TFs

    def matches(
        self,
        *,
        dry_run: bool,
        account_id: str = "",
        timeframe: str = "",
    ) -> bool:
        if not self.enabled:
            return False
        if not (self.bot_token or "").strip() or not (self.chat_id or "").strip():
            return False
        mode = (self.mode or MODE_BOTH).strip().lower()
        if mode == MODE_LIVE and dry_run:
            return False
        if mode == MODE_DRY_RUN and not dry_run:
            return False
        if self.account_ids:
            aid = (account_id or "").strip()
            if aid and aid not in self.account_ids:
                return False
        if self.timeframes:
            tf = (timeframe or "").strip()
            if tf and tf not in self.timeframes:
                return False
        return True


@dataclass
class NotificationEvent:
    id: str
    ts: str
    event: str
    mode: str  # live | dry_run
    symbol: str
    timeframe: str
    account_id: str
    account_name: str
    direction: str
    bot_id: str
    bot_name: str
    detail: str = ""
    ok: Optional[bool] = None


class NotificationManager:
    """Fan-out order alerts to matching Telegram bots; keep a recent event list."""

    def __init__(
        self,
        bots: Optional[Sequence[NotificationBot]] = None,
        *,
        log: Optional[LogFn] = None,
        max_events: int = 200,
    ):
        self._lock = threading.RLock()
        self._bots: List[NotificationBot] = list(bots or [])
        self._log = log or (lambda _m: None)
        self._max_events = max(20, int(max_events))
        self._events: List[NotificationEvent] = []

    # ---- bots CRUD -----------------------------------------------------

    def list_bots(self) -> List[NotificationBot]:
        with self._lock:
            return list(self._bots)

    def set_bots(self, bots: Sequence[NotificationBot]) -> None:
        with self._lock:
            self._bots = list(bots)

    def upsert_bot(self, bot: NotificationBot) -> None:
        with self._lock:
            for i, existing in enumerate(self._bots):
                if existing.id == bot.id:
                    self._bots[i] = bot
                    return
            self._bots.append(bot)

    def remove_bot(self, bot_id: str) -> None:
        with self._lock:
            self._bots = [b for b in self._bots if b.id != bot_id]

    def matching_bots(
        self,
        *,
        dry_run: bool,
        account_id: str = "",
        timeframe: str = "",
    ) -> List[NotificationBot]:
        with self._lock:
            return [
                b for b in self._bots
                if b.matches(dry_run=dry_run, account_id=account_id, timeframe=timeframe)
            ]

    def to_snapshot(self) -> List[Dict[str, Any]]:
        """Serializable bot list for LiveRunConfig / worker processes."""
        with self._lock:
            return [
                {
                    "id": b.id,
                    "name": b.name,
                    "bot_token": b.bot_token,
                    "chat_id": b.chat_id,
                    "enabled": b.enabled,
                    "mode": b.mode,
                    "account_ids": list(b.account_ids),
                    "timeframes": list(b.timeframes),
                }
                for b in self._bots
            ]

    @classmethod
    def from_snapshot(
        cls,
        rows: Optional[Sequence[Dict[str, Any]]],
        *,
        log: Optional[LogFn] = None,
    ) -> "NotificationManager":
        from .store import bot_from_dict

        bots = [bot_from_dict(r) for r in (rows or []) if isinstance(r, dict)]
        return cls(bots, log=log)

    # ---- events list (dashboard filter) --------------------------------

    def list_events(
        self,
        *,
        mode: Optional[str] = None,
        account_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[NotificationEvent]:
        with self._lock:
            rows = list(self._events)
        if mode in (MODE_LIVE, MODE_DRY_RUN):
            rows = [e for e in rows if e.mode == mode]
        if account_id:
            rows = [e for e in rows if e.account_id == account_id]
        return rows[: max(1, int(limit))]

    def _record(self, ev: NotificationEvent) -> None:
        with self._lock:
            self._events.insert(0, ev)
            if len(self._events) > self._max_events:
                self._events = self._events[: self._max_events]

    # ---- dispatch ------------------------------------------------------

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
        account_id: str = "",
        account_name: str = "",
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
        if event not in ORDER_EVENTS:
            return
        # Failures / safety blocks follow the dry_run flag of the attempt
        is_dry = bool(dry_run) or event == "DRY_RUN"
        targets = self.matching_bots(
            dry_run=is_dry,
            account_id=account_id,
            timeframe=timeframe,
        )
        if not targets:
            return
        common = dict(
            symbol=symbol,
            timeframe=timeframe,
            pattern=pattern,
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target,
            dry_run=is_dry,
            order_mode=order_mode,
            mt5_order_id=mt5_order_id,
            mt5_message=mt5_message,
            account_name=account_name or account_id,
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
        for bot in targets:
            text = format_order_alert(
                event,
                bot_name=bot.name,
                **common,
            )
            ev = NotificationEvent(
                id=uuid.uuid4().hex[:12],
                ts=datetime.now().isoformat(timespec="seconds"),
                event=event,
                mode=MODE_DRY_RUN if is_dry else MODE_LIVE,
                symbol=symbol,
                timeframe=timeframe,
                account_id=account_id,
                account_name=account_name or account_id,
                direction=direction,
                bot_id=bot.id,
                bot_name=bot.name,
                detail=mt5_message or close_reason or "",
            )
            self._record(ev)
            notifier = TelegramNotifier(
                enabled=True,
                bot_token=bot.bot_token,
                chat_id=bot.chat_id,
                log=self._log,
                bot_name=bot.name,
            )
            notifier.notify_order_event(event, **common)
            _ = text

    def update_from_legacy_single(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
    ) -> None:
        """Keep LiveRunConfig single-bot fields in sync as a both-mode fallback bot."""
        token = (bot_token or "").strip()
        cid = (chat_id or "").strip()
        with self._lock:
            legacy = next((b for b in self._bots if b.id == "legacy-default"), None)
            if not token or not cid:
                if legacy:
                    legacy.enabled = False
                return
            if legacy is None:
                self._bots.insert(
                    0,
                    NotificationBot(
                        id="legacy-default",
                        name="Default (legacy)",
                        bot_token=token,
                        chat_id=cid,
                        enabled=bool(enabled),
                        mode=MODE_BOTH,
                    ),
                )
            else:
                legacy.bot_token = token
                legacy.chat_id = cid
                legacy.enabled = bool(enabled)
