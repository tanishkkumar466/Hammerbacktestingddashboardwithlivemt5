"""Tests for multi-bot NotificationManager routing."""

from datetime import datetime

from notification.manager import (
    MODE_DRY_RUN,
    MODE_LIVE,
    NotificationBot,
    NotificationManager,
    in_notify_hours,
)


def _bot(**kwargs) -> NotificationBot:
    defaults = dict(
        id="b1",
        name="Bot",
        bot_token="token",
        chat_id="111",
        enabled=True,
        mode=MODE_LIVE,
    )
    defaults.update(kwargs)
    return NotificationBot(**defaults)


def test_matching_live_vs_dry():
    live_bot = _bot(id="live", name="Live", mode=MODE_LIVE, chat_id="1")
    dry_bot = _bot(id="dry", name="Dry", mode=MODE_DRY_RUN, chat_id="2")
    mgr = NotificationManager([live_bot, dry_bot])
    assert [b.id for b in mgr.matching_bots(dry_run=False)] == ["live"]
    assert [b.id for b in mgr.matching_bots(dry_run=True)] == ["dry"]


def test_matching_account_and_timeframe_filters():
    bot = _bot(
        mode="both",
        account_ids=["acc-a"],
        timeframes=["1h", "15m"],
    )
    mgr = NotificationManager([bot])
    assert mgr.matching_bots(dry_run=False, account_id="acc-a", timeframe="1h")
    assert not mgr.matching_bots(dry_run=False, account_id="acc-b", timeframe="1h")
    assert not mgr.matching_bots(dry_run=False, account_id="acc-a", timeframe="5m")


def test_in_notify_hours_empty_means_always():
    assert in_notify_hours("", "", now=datetime(2026, 1, 1, 3, 0))
    assert in_notify_hours("", "", now=datetime(2026, 1, 1, 15, 0))


def test_in_notify_hours_same_day_and_overnight():
    assert in_notify_hours("09:00", "22:00", now=datetime(2026, 1, 1, 12, 0))
    assert not in_notify_hours("09:00", "22:00", now=datetime(2026, 1, 1, 23, 0))
    assert in_notify_hours("22:00", "07:00", now=datetime(2026, 1, 1, 23, 30))
    assert in_notify_hours("22:00", "07:00", now=datetime(2026, 1, 1, 6, 0))
    assert not in_notify_hours("22:00", "07:00", now=datetime(2026, 1, 1, 12, 0))


def test_matching_respects_notify_hours():
    bot = _bot(
        mode=MODE_LIVE,
        notify_start_hhmm="09:00",
        notify_end_hhmm="17:00",
    )
    mgr = NotificationManager([bot])
    noon = datetime(2026, 6, 1, 12, 0)
    night = datetime(2026, 6, 1, 23, 0)
    assert mgr.matching_bots(dry_run=False, now=noon)
    assert not mgr.matching_bots(dry_run=False, now=night)


def test_dispatch_skips_outside_notify_hours(monkeypatch):
    sent = []
    logs = []

    def fake_notify(self, event, **kwargs):
        sent.append(event)

    monkeypatch.setattr(
        "notification.telegram.TelegramNotifier.notify_order_event",
        fake_notify,
    )
    bot = _bot(
        id="live",
        name="LiveChan",
        mode=MODE_LIVE,
        chat_id="1",
        notify_start_hhmm="09:00",
        notify_end_hhmm="17:00",
    )
    mgr = NotificationManager([bot], log=logs.append)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 1, 23, 15)

    monkeypatch.setattr("notification.manager.datetime", _FixedDateTime)
    mgr.notify_order_event(
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
        account_id="a1",
        account_name="Demo",
    )
    assert sent == []
    assert any("outside notify hours" in m for m in logs)
    assert mgr.list_events(mode=MODE_LIVE) == []


def test_dispatch_records_events(monkeypatch):
    sent = []

    def fake_notify(self, event, **kwargs):
        sent.append((self._bot_name, event, kwargs.get("dry_run"), kwargs.get("account_name")))

    monkeypatch.setattr(
        "notification.telegram.TelegramNotifier.notify_order_event",
        fake_notify,
    )
    live_bot = _bot(id="live", name="LiveChan", mode=MODE_LIVE, chat_id="1")
    dry_bot = _bot(id="dry", name="DryChan", mode=MODE_DRY_RUN, chat_id="2")
    mgr = NotificationManager([live_bot, dry_bot])
    mgr.notify_order_event(
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
        account_id="a1",
        account_name="Demo",
    )
    mgr.notify_order_event(
        "DRY_RUN",
        symbol="XAUUSD",
        timeframe="15m",
        pattern="Hammer",
        direction="SELL",
        volume=0.02,
        entry_price=1.0,
        stop_loss=1.1,
        target=0.8,
        dry_run=True,
        account_id="a1",
        account_name="Demo",
    )
    assert len(sent) == 2
    assert sent[0][0] == "LiveChan" and sent[0][2] is False
    assert sent[1][0] == "DryChan" and sent[1][2] is True
    live_events = mgr.list_events(mode=MODE_LIVE)
    dry_events = mgr.list_events(mode=MODE_DRY_RUN)
    assert len(live_events) == 1 and live_events[0].bot_name == "LiveChan"
    assert len(dry_events) == 1 and dry_events[0].bot_name == "DryChan"
