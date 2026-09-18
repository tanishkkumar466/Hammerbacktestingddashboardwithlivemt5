"""Tests for Telegram live notifications."""

import json
from unittest.mock import MagicMock, patch

import telegram_notify as tg


def test_format_order_alert_order():
    text = tg.format_order_alert(
        "ORDER",
        symbol="XAUUSD",
        timeframe="1h",
        pattern="Hammer",
        direction="BUY",
        volume=0.01,
        entry_price=2654.2,
        stop_loss=2648.0,
        target=2664.5,
        order_mode="market",
        mt5_order_id=12345,
        mt5_message="Order placed #12345",
        risk=6.2,
        rr_multiple=1.66,
        magic=24001,
        pattern_variant="CLASSIC",
        account_name="Demo",
        signal_bar_time="2026-03-18 10:00:00",
    )
    assert "ENTRY — ORDER PLACED" in text
    assert "XAUUSD" in text
    assert "BUY" in text
    assert "12345" in text
    assert "Hammer (CLASSIC)" in text
    assert "Magic 24001" in text
    assert "RR 1:1.66" in text
    assert "Demo" in text


def test_format_order_alert_dry_run():
    text = tg.format_order_alert(
        "DRY_RUN",
        symbol="XAUUSD",
        timeframe="1h",
        pattern="Hammer",
        direction="SELL",
        volume=0.02,
        entry_price=2650.0,
        stop_loss=2656.0,
        target=2638.0,
        dry_run=True,
        mt5_message="dry_run",
    )
    assert "DRY RUN" in text
    assert "[DRY RUN]" in text
    assert "SELL" in text
    assert "ENTRY SIGNAL — DRY RUN" in text
    assert "entry conditions only" in text
    assert "No order sent" in text


def test_format_order_alert_live_lane():
    text = tg.format_order_alert(
        "ORDER",
        symbol="XAUUSD",
        timeframe="1h",
        pattern="Hammer",
        direction="BUY",
        volume=0.01,
        entry_price=1.0,
        stop_loss=0.9,
        target=1.2,
        dry_run=False,
        account_name="IC Markets",
    )
    assert "[LIVE]" in text
    assert "IC Markets" in text


def test_format_order_alert_exit():
    text = tg.format_order_alert(
        "EXIT",
        symbol="XAUUSD",
        timeframe="1h",
        pattern="Hammer",
        direction="BUY",
        volume=0.01,
        entry_price=2650.0,
        stop_loss=2640.0,
        target=2670.0,
        exit_price=2670.0,
        profit=20.0,
        profit_currency="USD",
        close_reason="Take profit",
        magic=99,
        mt5_order_id=555,
    )
    assert "EXIT — POSITION CLOSED" in text
    assert "Exit:" in text
    assert "+20.00 USD" in text
    assert "Take profit" in text
    assert "Ticket #555" in text


@patch("notification.telegram.urllib.request.urlopen")
def test_send_message_success(mock_urlopen):
    resp = MagicMock()
    resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 1}}).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = resp

    ok, detail = tg.send_message("token123", "999888", "hello", max_retries=1)
    assert ok is True
    assert detail == "sent"


@patch("notification.telegram.time.sleep")
@patch("notification.telegram.urllib.request.urlopen")
def test_send_message_retries_then_succeeds(mock_urlopen, mock_sleep):
    fail_resp = MagicMock()
    fail_resp.read.return_value = json.dumps({"ok": False, "description": "temporary"}).encode()
    fail_resp.__enter__ = MagicMock(return_value=fail_resp)
    fail_resp.__exit__ = MagicMock(return_value=False)

    ok_resp = MagicMock()
    ok_resp.read.return_value = json.dumps({"ok": True}).encode()
    ok_resp.__enter__ = MagicMock(return_value=ok_resp)
    ok_resp.__exit__ = MagicMock(return_value=False)

    mock_urlopen.side_effect = [fail_resp, ok_resp]

    ok, detail = tg.send_message("token", "123", "hi", max_retries=2, retry_delay_sec=0.01)
    assert ok is True
    assert mock_sleep.called


def test_notifier_skips_when_disabled():
    log = MagicMock()
    n = tg.TelegramNotifier(enabled=False, bot_token="t", chat_id="1", log=log)
    n.notify_order_event(
        "ORDER",
        symbol="XAUUSD",
        timeframe="1h",
        pattern="Hammer",
        direction="BUY",
        volume=0.01,
        entry_price=1.0,
        stop_loss=0.9,
        target=1.2,
    )
    log.assert_not_called()
