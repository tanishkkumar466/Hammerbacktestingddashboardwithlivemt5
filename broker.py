"""
broker.py — MetaTrader 5 connection and order execution for live trading.

Requires: pip install MetaTrader5 (Windows, MT5 terminal installed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import logic

# Dashboard timeframe label -> (MT5 constant name, logic/backtest folder label)
TIMEFRAME_MT5_MAP: Dict[str, Tuple[str, str]] = {
    "1h": ("TIMEFRAME_H1", "1hour"),
    "30m": ("TIMEFRAME_M30", "30min"),
    "15m": ("TIMEFRAME_M15", "15min"),
    "10m": ("TIMEFRAME_M10", "10min"),
    "5m": ("TIMEFRAME_M5", "5min"),
    "3m": ("TIMEFRAME_M3", "3min"),
    "1m": ("TIMEFRAME_M1", "1min"),
}


@dataclass
class BrokerCredentials:
    terminal_path: str = ""
    login: int = 0
    password: str = ""
    server: str = ""


class MT5Broker:
    """Thin wrapper around MetaTrader5 Python API."""

    def __init__(self):
        self._mt5: Any = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._mt5 is not None

    def connect(self, creds: BrokerCredentials) -> Tuple[bool, str]:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return False, (
                "MetaTrader5 package not installed. On Windows run:\n"
                "    pip install MetaTrader5"
            )

        self.disconnect()

        path = (creds.terminal_path or "").strip()
        if path:
            ok = mt5.initialize(path=path)
        else:
            ok = mt5.initialize()
        if not ok:
            err = mt5.last_error()
            return False, f"MT5 initialize() failed: {err}"

        if creds.login:
            authorized = mt5.login(
                login=int(creds.login),
                password=creds.password or "",
                server=(creds.server or "").strip() or None,
            )
            if not authorized:
                err = mt5.last_error()
                mt5.shutdown()
                return False, f"MT5 login failed: {err}"

        self._mt5 = mt5
        self._connected = True
        info = mt5.account_info()
        if info is not None:
            return True, f"Connected — account {info.login} @ {info.server}"
        return True, "Connected to MT5 terminal."

    def disconnect(self) -> None:
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
        self._mt5 = None
        self._connected = False

    def account_info_dict(self) -> Dict[str, Any]:
        if not self.is_connected:
            return {}
        info = self._mt5.account_info()
        if info is None:
            return {}
        return {
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "margin_free": info.margin_free,
            "currency": info.currency,
            "trade_mode": getattr(info, "trade_mode", None),
        }

    def account_is_demo(self) -> Optional[bool]:
        """True if demo account, False if real/live, None if unknown."""
        if not self.is_connected:
            return None
        info = self._mt5.account_info()
        if info is None:
            return None
        # MT5: 0 = demo, 2 = real (contest = 1)
        mode = int(getattr(info, "trade_mode", -1))
        if mode == 0:
            return True
        if mode == 2:
            return False
        return None

    def spread_points(self, symbol: str) -> Optional[float]:
        if not self.is_connected:
            return None
        info = self._mt5.symbol_info(symbol)
        tick = self._mt5.symbol_info_tick(symbol)
        if info is None or tick is None or info.point <= 0:
            return None
        return (float(tick.ask) - float(tick.bid)) / float(info.point)

    def ensure_symbol(self, symbol: str) -> Tuple[bool, str]:
        ok, msg, _resolved = self.resolve_and_ensure_symbol(symbol)
        return ok, msg

    def resolve_symbol_name(self, symbol: str) -> Optional[str]:
        """Find broker-exact symbol name (e.g. XAUUSD → XAUUSDm)."""
        if not self.is_connected:
            return (symbol or "").strip() or None
        sym = (symbol or "").strip()
        if not sym:
            return None
        mt5 = self._mt5
        if mt5.symbol_info(sym) is not None:
            return sym
        wanted = sym.upper()
        candidates: List[str] = []
        try:
            all_syms = mt5.symbols_get(group="*")
        except Exception:
            all_syms = mt5.symbols_get()
        if all_syms:
            for info in all_syms:
                name = getattr(info, "name", "") or ""
                if name.upper() == wanted:
                    return name
            for info in all_syms:
                name = getattr(info, "name", "") or ""
                nu = name.upper()
                if nu.startswith(wanted) or wanted.startswith(nu.replace(".", "")):
                    candidates.append(name)
            for suffix in ("m", ".m", "_m", "M", ".i", ".pro"):
                trial = sym + suffix
                if mt5.symbol_info(trial) is not None:
                    return trial
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                for c in candidates:
                    if c.upper().startswith(wanted):
                        return c
                return candidates[0]
        return None

    def resolve_and_ensure_symbol(self, symbol: str) -> Tuple[bool, str, str]:
        """Resolve broker symbol name and enable it in Market Watch."""
        if not self.is_connected:
            return False, "Not connected.", (symbol or "").strip()
        requested = (symbol or "").strip()
        if not requested:
            return False, "Symbol is empty.", requested
        resolved = self.resolve_symbol_name(requested)
        if not resolved:
            return (
                False,
                f"Symbol '{requested}' not found on this broker. "
                "Use the exact name from MT5 Market Watch (e.g. XAUUSD vs XAUUSDm).",
                requested,
            )
        info = self._mt5.symbol_info(resolved)
        if info is None:
            return False, f"Symbol '{resolved}' unavailable.", requested
        if not info.visible and not self._mt5.symbol_select(resolved, True):
            return False, f"Could not enable '{resolved}' in Market Watch.", requested
        note = f" (resolved from {requested})" if resolved != requested else ""
        return True, f"Symbol ready: {resolved}{note}", resolved

    def fetch_rates(self, symbol: str, timeframe_label: str, count: int = 400) -> Tuple[List[logic.Candle], Optional[logic.Candle], str]:
        """
        Returns (closed_candles chronological, forming_candle, error_msg).
        MT5 index 0 is the current forming bar.
        """
        if not self.is_connected:
            return [], None, "Not connected."
        ok, msg, resolved = self.resolve_and_ensure_symbol(symbol)
        if not ok:
            return [], None, msg
        sym = resolved
        if timeframe_label not in TIMEFRAME_MT5_MAP:
            return [], None, f"Unknown timeframe: {timeframe_label}"
        tf_name, _ = TIMEFRAME_MT5_MAP[timeframe_label]
        tf_const = getattr(self._mt5, tf_name)
        rates = self._mt5.copy_rates_from_pos(sym, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            err = self._mt5.last_error()
            return [], None, f"No rates for {sym} {timeframe_label}: {err}"

        def _to_candle(r) -> logic.Candle:
            ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).replace(tzinfo=None)
            return logic.Candle(
                timestamp=ts,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
            )

        forming = _to_candle(rates[0])
        closed_rev = [_to_candle(r) for r in rates[1:]]
        closed = list(reversed(closed_rev))
        return closed, forming, ""

    def count_open_positions(self, symbol: str, magic: int) -> int:
        if not self.is_connected:
            return 0
        positions = self._mt5.positions_get(symbol=symbol)
        if positions is None:
            return 0
        return sum(1 for p in positions if int(p.magic) == int(magic))

    def _filling_modes_for_symbol(self, sym_info) -> List[int]:
        """Broker-supported order filling modes, most preferred first."""
        mt5 = self._mt5
        modes: List[int] = []
        filling = int(getattr(sym_info, "filling_mode", 0) or 0)
        candidates = (
            (getattr(mt5, "SYMBOL_FILLING_FOK", 1), getattr(mt5, "ORDER_FILLING_FOK", 0)),
            (getattr(mt5, "SYMBOL_FILLING_IOC", 2), getattr(mt5, "ORDER_FILLING_IOC", 1)),
            (getattr(mt5, "SYMBOL_FILLING_RETURN", 4), getattr(mt5, "ORDER_FILLING_RETURN", 2)),
        )
        for flag, order_fill in candidates:
            if order_fill and (filling & flag):
                modes.append(order_fill)
        if not modes:
            for order_fill in (
                getattr(mt5, "ORDER_FILLING_IOC", None),
                getattr(mt5, "ORDER_FILLING_FOK", None),
                getattr(mt5, "ORDER_FILLING_RETURN", None),
            ):
                if order_fill is not None:
                    modes.append(order_fill)
        # de-dupe preserving order
        seen = set()
        out: List[int] = []
        for m in modes:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _normalize_volume(self, sym_info, volume: float) -> float:
        volume = max(sym_info.volume_min, min(sym_info.volume_max, float(volume)))
        step = float(sym_info.volume_step or 0.01)
        if step > 0:
            volume = round(volume / step) * step
        return float(round(volume, 8))

    def _normalize_price(self, sym_info, price: float) -> float:
        digits = int(getattr(sym_info, "digits", 5) or 5)
        return round(float(price), digits)

    def terminal_allows_trading(self) -> Tuple[bool, str]:
        if not self.is_connected:
            return False, "Not connected."
        term = self._mt5.terminal_info()
        if term is None:
            return False, "MT5 terminal_info() unavailable."
        if not getattr(term, "trade_allowed", True):
            return False, (
                "AutoTrading disabled in MT5 — click 'Algo Trading' on the toolbar "
                "or Tools → Options → Expert Advisors → Allow algorithmic trading."
            )
        if getattr(term, "dlls_allowed", True) is False:
            return False, "DLL imports disabled in MT5 terminal settings."
        return True, ""

    def _symbol_allows_trading(self, sym_info) -> Tuple[bool, str]:
        if sym_info is None:
            return False, "Symbol info unavailable."
        mode = int(getattr(sym_info, "trade_mode", 4) or 4)
        disabled = getattr(self._mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
        close_only = getattr(self._mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3)
        if mode == disabled:
            return False, "Symbol trading is disabled on this broker."
        if mode == close_only:
            return False, "Symbol is close-only — new entries not allowed."
        return True, ""

    def _prepare_sl_tp(
        self,
        sym_info,
        direction: logic.TradeDirection,
        ref_price: float,
        sl: float,
        tp: float,
    ) -> Tuple[float, float, Optional[str]]:
        """Normalize prices and enforce broker minimum stop distance (trade_stops_level)."""
        sl_n = self._normalize_price(sym_info, float(sl)) if sl else 0.0
        tp_n = self._normalize_price(sym_info, float(tp)) if tp else 0.0
        ref = float(ref_price)
        point = float(getattr(sym_info, "point", 0.0) or 0.0)
        stops_level = int(getattr(sym_info, "trade_stops_level", 0) or 0)
        if point <= 0 or stops_level <= 0:
            return sl_n, tp_n, None
        min_dist = stops_level * point
        if direction == logic.TradeDirection.BUY:
            if sl_n > 0 and (ref - sl_n) < min_dist - point * 0.01:
                return sl_n, tp_n, (
                    f"Stop loss too close to price for {getattr(sym_info, 'name', '?')} "
                    f"(broker min {stops_level} points)."
                )
            if tp_n > 0 and (tp_n - ref) < min_dist - point * 0.01:
                return sl_n, tp_n, (
                    f"Take profit too close to price (broker min {stops_level} points)."
                )
        else:
            if sl_n > 0 and (sl_n - ref) < min_dist - point * 0.01:
                return sl_n, tp_n, (
                    f"Stop loss too close to price (broker min {stops_level} points)."
                )
            if tp_n > 0 and (ref - tp_n) < min_dist - point * 0.01:
                return sl_n, tp_n, (
                    f"Take profit too close to price (broker min {stops_level} points)."
                )
        return sl_n, tp_n, None

    def _compute_limit_price(
        self,
        sym_info,
        direction: logic.TradeDirection,
        entry_price: float,
        offset_points: float,
    ) -> float:
        point = float(getattr(sym_info, "point", 0.0) or 0.0)
        offset = float(offset_points) * point
        if direction == logic.TradeDirection.BUY:
            return self._normalize_price(sym_info, entry_price - offset)
        return self._normalize_price(sym_info, entry_price + offset)

    def send_trade_order(
        self,
        symbol: str,
        direction: logic.TradeDirection,
        volume: float,
        sl: float,
        tp: float,
        magic: int,
        *,
        order_mode: str = "market",
        limit_price: Optional[float] = None,
        limit_offset_points: float = 0.0,
        comment: str = "HammerLive",
        deviation: int = 20,
    ) -> Tuple[bool, str, Optional[int]]:
        mode = (order_mode or "market").strip().lower()
        if mode not in ("market", "limit_entry", "limit_offset"):
            return False, f"Unknown order_mode {order_mode!r}.", None
        if mode == "market":
            return self.send_market_order(
                symbol, direction, volume, sl, tp, magic,
                comment=comment, deviation=deviation,
            )
        if limit_price is None:
            return False, "Limit order requires a price (strategy entry).", None
        lp = float(limit_price)
        if mode == "limit_offset":
            sym_info = self._mt5.symbol_info(symbol) if self.is_connected else None
            if sym_info is None:
                return False, f"Symbol {symbol} unavailable.", None
            lp = self._compute_limit_price(sym_info, direction, lp, limit_offset_points)
        return self.send_limit_order(
            symbol, direction, volume, lp, sl, tp, magic,
            comment=comment, deviation=deviation,
        )

    def send_market_order(
        self,
        symbol: str,
        direction: logic.TradeDirection,
        volume: float,
        sl: float,
        tp: float,
        magic: int,
        comment: str = "HammerLive",
        deviation: int = 20,
    ) -> Tuple[bool, str, Optional[int]]:
        if not self.is_connected:
            return False, "Not connected.", None

        ok_term, term_msg = self.terminal_allows_trading()
        if not ok_term:
            return False, term_msg, None

        ok_sym, msg, resolved = self.resolve_and_ensure_symbol(symbol)
        if not ok_sym:
            return False, msg, None
        symbol = resolved

        sym_info = self._mt5.symbol_info(symbol)
        if sym_info is None:
            return False, f"Symbol {symbol} unavailable.", None

        ok_trade, trade_msg = self._symbol_allows_trading(sym_info)
        if not ok_trade:
            return False, trade_msg, None

        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, "No tick data.", None

        if direction == logic.TradeDirection.BUY:
            order_type = self._mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
        else:
            order_type = self._mt5.ORDER_TYPE_SELL
            price = float(tick.bid)

        volume = self._normalize_volume(sym_info, volume)
        if volume < float(sym_info.volume_min):
            return False, f"Volume {volume} below symbol minimum {sym_info.volume_min}.", None

        sl, tp, stop_err = self._prepare_sl_tp(sym_info, direction, price, sl, tp)
        if stop_err:
            return False, stop_err, None

        filling_modes = self._filling_modes_for_symbol(sym_info)
        last_err = "No filling mode available"
        retcode_unsupported = getattr(self._mt5, "TRADE_RETCODE_INVALID_FILL", 10030)

        for type_filling in filling_modes:
            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": float(sl),
                "tp": float(tp),
                "deviation": deviation,
                "magic": int(magic),
                "comment": comment[:31],
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": type_filling,
            }

            result = self._mt5.order_send(request)
            if result is None:
                last_err = f"order_send failed: {self._mt5.last_error()}"
                continue

            if result.retcode == self._mt5.TRADE_RETCODE_DONE:
                order_id = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
                return True, f"Order placed #{order_id} vol={volume} @ {price}", order_id or None

            last_err = f"Order rejected: {result.retcode} — {result.comment}"
            if result.retcode != retcode_unsupported:
                return False, last_err, None

        return False, (
            f"{last_err}. Enable Algo Trading in MT5 (toolbar) and check symbol is tradable."
        ), None

    def send_limit_order(
        self,
        symbol: str,
        direction: logic.TradeDirection,
        volume: float,
        limit_price: float,
        sl: float,
        tp: float,
        magic: int,
        comment: str = "HammerLive",
        deviation: int = 20,
    ) -> Tuple[bool, str, Optional[int]]:
        if not self.is_connected:
            return False, "Not connected.", None

        ok_term, term_msg = self.terminal_allows_trading()
        if not ok_term:
            return False, term_msg, None

        ok_sym, msg, resolved = self.resolve_and_ensure_symbol(symbol)
        if not ok_sym:
            return False, msg, None
        symbol = resolved

        sym_info = self._mt5.symbol_info(symbol)
        if sym_info is None:
            return False, f"Symbol {symbol} unavailable.", None

        ok_trade, trade_msg = self._symbol_allows_trading(sym_info)
        if not ok_trade:
            return False, trade_msg, None

        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, "No tick data.", None

        price = self._normalize_price(sym_info, limit_price)
        if direction == logic.TradeDirection.BUY:
            order_type = self._mt5.ORDER_TYPE_BUY_LIMIT
            if price >= float(tick.ask):
                return False, (
                    f"Buy limit price {price} must be below ask {tick.ask}. "
                    "Use Market or adjust offset."
                ), None
        else:
            order_type = self._mt5.ORDER_TYPE_SELL_LIMIT
            if price <= float(tick.bid):
                return False, (
                    f"Sell limit price {price} must be above bid {tick.bid}. "
                    "Use Market or adjust offset."
                ), None

        volume = self._normalize_volume(sym_info, volume)
        if volume < float(sym_info.volume_min):
            return False, f"Volume {volume} below symbol minimum {sym_info.volume_min}.", None

        sl, tp, stop_err = self._prepare_sl_tp(sym_info, direction, price, sl, tp)
        if stop_err:
            return False, stop_err, None

        filling_modes = self._filling_modes_for_symbol(sym_info)
        last_err = "No filling mode available"
        retcode_unsupported = getattr(self._mt5, "TRADE_RETCODE_INVALID_FILL", 10030)

        for type_filling in filling_modes:
            request = {
                "action": self._mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": float(sl),
                "tp": float(tp),
                "deviation": deviation,
                "magic": int(magic),
                "comment": comment[:31],
                "type_time": self._mt5.ORDER_TIME_GTC,
                "type_filling": type_filling,
            }
            result = self._mt5.order_send(request)
            if result is None:
                last_err = f"order_send failed: {self._mt5.last_error()}"
                continue
            if result.retcode == self._mt5.TRADE_RETCODE_DONE:
                order_id = int(getattr(result, "order", 0) or 0)
                return True, f"Limit order placed #{order_id} @ {price} vol={volume}", order_id or None
            last_err = f"Limit rejected: {result.retcode} — {result.comment}"
            if result.retcode != retcode_unsupported:
                return False, last_err, None

        return False, f"{last_err}. Check limit price vs market.", None
