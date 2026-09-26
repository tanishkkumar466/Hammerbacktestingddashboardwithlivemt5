"""Tests for Telegram live notifications."""

import json
from unittest.mock import MagicMock, patch

from notification import telegram as tg


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
    lines = text.splitlines()
    assert lines[0] == "<b>Hammer (Classic) — BUY</b>"
    assert lines[1] == "ORDER PLACED · LIVE"
    assert "<b>Entry:  2654.20</b>" in text
    assert "<b>SL:  2648.00</b>   (risk 6.20)" in text
    assert "<b>TP:  2664.50</b>   (RR 1:1.66)" in text
    assert "XAUUSD · 1h · 0.01 lot · Market" in text
    assert "Ticket #12345" in text
    assert "<i>Account: Demo · Magic 24001</i>" in text


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
    lines = text.splitlines()
    assert lines[0] == "<b>Hammer — SELL</b>"
    assert lines[1] == "DRY RUN — not placed (no exit alert)"
    assert "Ticket" not in text and "Market" not in text
    assert "dry_run" not in text


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
    assert "ORDER PLACED · LIVE" in text
    assert "IC Markets" in text
    assert "<b>Entry:  1.0000</b>" in text
    assert "(risk 0.1000)" in text


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
    lines = text.splitlines()
    assert lines[0] == "<b>Hammer — BUY closed — PROFIT</b>"
    assert lines[1] == "EXIT · Take profit · LIVE"
    assert "<b>P/L:  +20.00 USD</b>" in text
    assert "<b>Exit:  2670.00</b>" in text
    assert "Plan was SL 2640.00 · TP 2670.00" in text
    assert "XAUUSD · 1h · 0.01 lot · Ticket #555" in text


_BASE = dict(
    symbol="XAUUSD", timeframe="5m", direction="BUY", volume=0.1,
    entry_price=2652.0, stop_loss=2639.7, target=2676.6,
)


def test_short_strategy_names():
    assert tg.short_strategy_name("Hammer with candles") == "HWC"
    assert tg.short_strategy_name("Hammer with candle 35%") == "HWC 35%"
    assert tg.short_strategy_name("Hammer") == "Hammer"
    assert tg.short_strategy_name("Doji") == "Doji"
    assert tg.short_strategy_name("") == "Strategy"


def test_alert_starts_with_strategy_and_preset():
    text = tg.format_order_alert(
        "ORDER", pattern="Hammer with candle 35%",
        preset_name=r"C:\presets\gold_5m_aggressive.json", **_BASE,
    )
    lines = text.splitlines()
    assert lines[0] == "<b>HWC 35% — BUY</b>"
    assert lines[1] == "Preset: <b>gold_5m_aggressive</b>"
    assert lines[2] == "ORDER PLACED · LIVE"
    assert lines[4:7] == [
        "<b>Entry:  2652.00</b>",
        "<b>SL:  2639.70</b>   (risk 12.30)",
        "<b>TP:  2676.60</b>   (RR 1:2.00)",
    ]


def test_no_preset_line_when_preset_unknown():
    text = tg.format_order_alert("ORDER", pattern="Hammer with candles", **_BASE)
    assert "Preset:" not in text
    assert text.splitlines()[0] == "<b>HWC — BUY</b>"


def test_no_emojis_in_alert():
    for event in tg.ORDER_EVENTS:
        text = tg.format_order_alert(event, pattern="Hammer", profit=-5.0, exit_price=2650.0, **_BASE)
        assert all(ord(ch) < 0x1F000 for ch in text)


def test_failure_reason_is_near_the_top():
    text = tg.format_order_alert(
        "ORDER_FAIL", pattern="Hammer", mt5_message="Invalid stops (10016)", **_BASE,
    )
    lines = text.splitlines()
    assert lines[1] == "ENTRY FAILED · LIVE"
    assert lines[2] == "Reason: <b>Invalid stops (10016)</b>"


def test_exit_loss_is_spelled_out():
    text = tg.format_order_alert(
        "EXIT", pattern="Hammer", exit_price=2639.7, profit=-123.0, profit_currency="USD", **_BASE,
    )
    assert text.splitlines()[0] == "<b>Hammer — BUY closed — LOSS</b>"
    assert "<b>P/L:  -123.00 USD</b>" in text


def test_user_text_is_html_escaped():
    text = tg.format_order_alert(
        "ORDER", pattern="Hammer", account_name="A<b>&", preset_name="x<y>.json",
        mt5_message="done <ok>", **_BASE,
    )
    assert "A&lt;b&gt;&amp;" in text
    assert "Preset: <b>x&lt;y&gt;</b>" in text
    assert "done &lt;ok&gt;" in text


def test_less_important_lines_are_italic_at_bottom():
    text = tg.format_order_alert(
        "ORDER", pattern="Hammer", account_name="Demo", magic=7, bot_name="Main",
        signal_bar_time="2026-09-26 10:05:00", entry_bar_time="2026-09-26 10:10:00", **_BASE,
    )
    tail = text.splitlines()[-3:]
    assert tail == [
        "<i>Account: Demo · Magic 7</i>",
        "<i>Signal bar: 26 Sep 10:05 · Entry bar: 10:10</i>",
        "<i>Bot: Main</i>",
    ]


def _resp(payload):
    r = MagicMock()
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = MagicMock(return_value=r)
    r.__exit__ = MagicMock(return_value=False)
    return r


@patch("notification.telegram.urllib.request.urlopen")
def test_send_message_html_sends_parse_mode(mock_urlopen):
    mock_urlopen.return_value = _resp({"ok": True})
    ok, _ = tg.send_message("t", "1", "<b>hi</b>", max_retries=1, parse_mode="HTML")
    assert ok
    body = mock_urlopen.call_args.args[0].data.decode()
    assert "parse_mode=HTML" in body


@patch("notification.telegram.time.sleep")
@patch("notification.telegram.urllib.request.urlopen")
def test_send_message_falls_back_to_plain_text_when_markup_rejected(mock_urlopen, _sleep):
    mock_urlopen.side_effect = [
        _resp({"ok": False, "description": "Bad Request: can't parse entities: unclosed tag"}),
        _resp({"ok": True}),
    ]
    ok, detail = tg.send_message("t", "1", "<b>Entry &amp; SL</b>", max_retries=3, parse_mode="HTML")
    assert ok and "plain text" in detail
    assert mock_urlopen.call_count == 2  # no pointless retries of the bad markup
    plain_body = mock_urlopen.call_args_list[1].args[0].data.decode()
    assert "parse_mode" not in plain_body
    assert "Entry+%26+SL" in plain_body


@patch("notification.telegram.urllib.request.urlopen")
def test_plain_send_has_no_parse_mode(mock_urlopen):
    mock_urlopen.return_value = _resp({"ok": True})
    tg.send_message("t", "1", "Test <message>", max_retries=1)
    assert "parse_mode" not in mock_urlopen.call_args.args[0].data.decode()


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
    # SSL context must be passed (certifi / verify path)
    assert mock_urlopen.call_args.kwargs.get("context") is not None


@patch("notification.telegram.urllib.request.urlopen")
def test_send_message_ssl_verify_fallback(mock_urlopen):
    import ssl
    import urllib.error

    ok_resp = MagicMock()
    ok_resp.read.return_value = json.dumps({"ok": True}).encode()
    ok_resp.__enter__ = MagicMock(return_value=ok_resp)
    ok_resp.__exit__ = MagicMock(return_value=False)

    def _side_effect(*_a, **kwargs):
        ctx = kwargs.get("context")
        # First call uses verifying context → fail; insecure → succeed
        if ctx is not None and getattr(ctx, "check_hostname", True):
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError(
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                    "self-signed certificate in certificate chain"
                )
            )
        return ok_resp

    mock_urlopen.side_effect = _side_effect
    ok, detail = tg.send_message("token", "123", "hi", max_retries=1)
    assert ok is True
    assert "relaxed" in detail.lower() or detail == "sent"


def test_build_ssl_context_insecure():
    ctx = tg.build_ssl_context(insecure=True)
    assert ctx.check_hostname is False



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
