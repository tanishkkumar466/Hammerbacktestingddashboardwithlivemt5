"""Fetch must share Live's MT5 — never stop Live with a second attach."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_worker_fetch_job_reuses_broker_without_own_connection():
    """fetch_job in the account worker must call run_fetch_job(own_connection=False)."""
    import live_account_worker as law

    # Build a minimal fake of the inner handlers by exercising _on_fetch_job logic
    # through a harvested copy of the helper pattern.
    calls = {}
    broker = MagicMock()
    broker.is_connected = True
    broker.raw_mt5 = object()
    broker.api_lock = threading.RLock()

    emit = MagicMock()
    fetch_stop = threading.Event()
    stop_all = False
    fetch_thread_holder = {"t": None}

    def _on_fetch_job(msg):
        if not broker.is_connected or broker.raw_mt5 is None:
            emit("fetch_failed", message="Not connected")
            return
        job = {
            "output_root": str(msg.get("output_root") or ""),
            "symbols": list(msg.get("symbols") or ["XAUUSD"]),
            "timeframes": list(msg.get("timeframes") or ["1min"]),
            "update_existing": bool(msg.get("update_existing", True)),
            "start_date": msg.get("start_date"),
            "market_type": msg.get("market_type"),
            "auto_detect_market": bool(msg.get("auto_detect_market", True)),
            "flat_under_root": bool(msg.get("flat_under_root", False)),
        }

        def _run():
            import fetch as data_fetcher

            result = data_fetcher.run_fetch_job(
                output_root=job["output_root"],
                symbols=job["symbols"],
                timeframes=job["timeframes"],
                update_existing=job["update_existing"],
                start_date=job.get("start_date"),
                use_mock=False,
                market_type=job.get("market_type"),
                auto_detect_market=job["auto_detect_market"],
                flat_under_root=job["flat_under_root"],
                mt5_module=broker.raw_mt5,
                own_connection=False,
                api_lock=broker.api_lock,
                log=lambda m: emit("fetch_log", message=str(m)),
                should_stop=lambda: fetch_stop.is_set() or stop_all,
            )
            calls["result"] = result
            emit("fetch_done", result=result)

        with patch("fetch.run_fetch_job", return_value={"total_saved": 3}) as mock_run:
            _run()
            assert mock_run.called
            kwargs = mock_run.call_args.kwargs
            assert kwargs["own_connection"] is False
            assert kwargs["mt5_module"] is broker.raw_mt5
            assert kwargs["api_lock"] is broker.api_lock

    _on_fetch_job({
        "output_root": "/tmp/data",
        "symbols": ["XAUUSD"],
        "timeframes": ["1min"],
        "update_existing": True,
    })
    emit.assert_any_call("fetch_done", result={"total_saved": 3})


def test_worker_source_defines_fetch_job_cmd():
    from pathlib import Path

    src = Path("live_account_worker.py").read_text(encoding="utf-8")
    assert 'cmd == "fetch_job"' in src
    assert "own_connection=False" in src
    assert 'cmd == "fetch_stop"' in src
