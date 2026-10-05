"""API-style refresh scenarios without network, keys, or third-party libraries."""

import csv
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fintech_bot.app import build_application
from fintech_bot.config import Settings, StrategySettings
from fintech_bot.data.base import CandleBatch
from fintech_bot.data.csv_provider import CsvDataProvider
from fintech_bot.data.replay import ReplayDataProvider
from fintech_bot.domain import Instrument, SignalSide, VIETNAM
from fintech_bot.market import Clock, MarketCalendar, ReplayClock
from fintech_bot.strategies.ema_rsi import EmaRsiStrategy
from tests.test_project import RecordingNotifier


class UpdatingProvider:
    source = "api-contract-test"
    is_demo = True

    def __init__(self, replay, symbols):
        self.series = {(symbol, frame): replay.get_candles(symbol, frame)
                       for symbol in symbols for frame in ("1d",)}

    def get_candles(self, symbol, timeframe):
        return list(self.series[symbol, timeframe])

    def get_financials(self, symbol):
        return None


class BatchProvider(UpdatingProvider):
    def __init__(self, replay, symbols):
        super().__init__(replay, symbols)
        self.calls = []
        self.errors = {}
        self.failure = None

    def get_candles_batch(self, symbols, timeframes):
        self.calls.append((symbols, timeframes))
        if self.failure:
            raise self.failure
        return CandleBatch({key: list(value) for key, value in self.series.items()
                            if key[0] in symbols and key[1] in timeframes}, self.errors.copy())

    def get_candles(self, symbol, timeframe):
        raise AssertionError("Batch provider must not fall back to single-symbol requests")


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.symbols = ("FPT", "HPG")
        self.settings = Settings(self.symbols, self.root / "test.sqlite3", StrategySettings(), timeframe="1d", scan_interval_seconds=60)
        self.clock = ReplayClock(datetime(2026, 9, 18, 10, tzinfo=VIETNAM))
        self.replay = ReplayDataProvider([Instrument(symbol, "HOSE") for symbol in self.symbols],
                                         self.clock, MarketCalendar())
        self.provider = UpdatingProvider(self.replay, self.symbols)
        self.notifier = RecordingNotifier()
        self.app = build_application(self.settings, self.notifier, self.clock, provider=self.provider)
        self.addCleanup(self.app.close)

    def test_injected_provider_defaults_to_real_clock(self):
        app = build_application(replace(self.settings, database_path=self.root / "clock.sqlite3"),
                                provider=self.provider)
        try:
            self.assertIs(type(app.clock), Clock)
            self.assertIs(app.scanner.provider, self.provider)
        finally:
            app.close()

    def test_unchanged_refresh_checks_source_but_skips_calculation_and_writes(self):
        self.app.scanner.run()
        changes = self.app.repository.connection.total_changes
        with patch.object(self.provider, "get_candles", wraps=self.provider.get_candles) as fetch, \
             patch.object(self.app.scanner.strategy, "evaluate", wraps=self.app.scanner.strategy.evaluate) as evaluate:
            report = self.app.scanner.run()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(evaluate.call_count, 0)
        self.assertEqual(len(report.signals), 2)
        self.assertEqual(self.app.repository.connection.total_changes, changes)

    def test_incomplete_daily_bar_does_not_replace_closed_history_or_alert(self):
        self.app.commands.handle("/subscribe FPT", "alice")
        self.app.commands.handle("/scan", "alice")
        self.clock.advance(300)
        previous = self.provider.series["FPT", "1d"][-1]
        new = replace(previous, observed_at=self.clock.now(),
                      open=130, high=131, low=9, close=10, is_closed=False)
        self.provider.series["FPT", "1d"][-1] = new
        self.app.commands.handle("/scan", "alice")
        self.assertIn("Lần quét mới nhất bị lỗi", self.app.commands.handle("/stock FPT", "alice"))
        saved = self.app.repository.load_candles(self.provider.source, "FPT", "1d")
        self.assertEqual(saved[-1], previous)
        self.assertFalse(any(item.symbol == "FPT" for item in self.app.scanner.latest_report.signals))
        messages = len(self.notifier.messages)
        self.assertEqual(messages, 1)
        self.app.commands.handle("/scan", "alice")
        self.assertEqual(len(self.notifier.messages), messages)

    def test_history_correction_recomputes_even_when_last_bar_unchanged(self):
        before = self.app.scanner.run().signals[0]
        series = self.provider.series["FPT", "1d"]
        revised = replace(series[-3], close=120, high=121)
        series[-3] = revised
        after = self.app.scanner.run().signals[0]
        self.assertEqual(before.closed_at, after.closed_at)
        self.assertNotEqual(before.indicators["ema_slow"], after.indicators["ema_slow"])
        stored = self.app.repository.load_candles(self.provider.source, "FPT", "1d")
        self.assertEqual(stored[-3], revised)

    def test_cache_does_not_hide_stale_source(self):
        self.app.scanner.run()
        self.clock.current = datetime(2026, 9, 21, 15, 1, tzinfo=VIETNAM)
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            result = self.app.scanner.run()
        self.assertFalse(result.signals)
        self.assertEqual(len(result.errors), 2)

    def test_fourteen_minute_old_data_is_ready_with_fifteen_minute_schedule(self):
        self.app.scanner.run()
        self.clock.advance(14 * 60)
        result = self.app.scanner.run()
        self.assertFalse(result.errors)
        self.assertEqual(len(result.signals), 2)

    def test_batch_is_once_per_scan_and_scoped_for_on_demand_lookup(self):
        provider = BatchProvider(self.replay, self.symbols)
        self.app.scanner.provider = provider
        self.assertFalse(self.app.scanner.run().errors)
        self.assertFalse(self.app.scanner.run(["FPT"]).errors)
        self.assertEqual(provider.calls, [(self.symbols, ("1d",)), (("FPT",), ("1d",))])

    def test_batch_partial_error_keeps_other_symbols_and_does_not_use_old_signal(self):
        provider = BatchProvider(self.replay, self.symbols)
        self.app.scanner.provider = provider
        self.app.scanner.run()
        provider.errors["FPT", "1d"] = "429: chờ lượt sau"
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            result = self.app.scanner.run()
        self.assertEqual(len(result.signals), 1)
        self.assertIn("429", result.errors["FPT/1d"])
        self.assertFalse(any(item.symbol == "FPT" for item in result.signals))

    def test_failed_or_incomplete_batch_never_falls_back_to_extra_requests(self):
        provider = BatchProvider(self.replay, self.symbols)
        self.app.scanner.provider = provider
        provider.failure = TimeoutError("timed out")
        result = self.app.scanner.run()
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(result.skipped), 2)
        self.assertFalse(result.errors)
        provider.failure = None
        del provider.series["FPT", "1d"]
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            result = self.app.scanner.run()
        self.assertIn("thiếu", result.errors["FPT/1d"])
        self.assertEqual(len(result.signals), 1)

    def test_strategy_replacement_invalidates_cached_evaluation(self):
        old = self.app.scanner.run().signals[0]
        self.app.scanner.strategy = EmaRsiStrategy(StrategySettings(ema_fast=10))
        new = self.app.scanner.run().signals[0]
        self.assertNotEqual(old.strategy_id, new.strategy_id)
        self.assertNotEqual(old.indicators["ema_fast"], new.indicators["ema_fast"])

    def test_future_observation_is_not_used_even_if_marked_closed(self):
        series = self.provider.series["FPT", "1d"]
        series[-1] = replace(series[-1], observed_at=self.clock.now() + timedelta(seconds=1))
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            report = self.app.scanner.run()
        self.assertNotIn("FPT", [item.symbol for item in report.signals])
        self.assertIn("FPT/1d", report.errors)

    def test_slow_scan_schedules_from_completion_not_start(self):
        fetch = self.provider.get_candles
        def slow(symbol, timeframe):
            self.clock.advance(20)
            return fetch(symbol, timeframe)
        self.provider.get_candles = slow
        self.assertTrue(self.app.scheduler.tick().scanned)
        self.assertFalse(self.app.scheduler.tick().scanned)
        self.assertEqual(self.app.scheduler.next_scan, self.clock.now().timestamp() + 60)


class FileAndCalendarCacheTests(unittest.TestCase):
    def test_calendar_cache_does_not_leak_mutations_or_holidays(self):
        day = datetime(2026, 9, 18).date()
        calendar = MarketCalendar()
        values = calendar.ends(day, "HOSE", "1d")
        original_length = len(values)
        values.clear()
        self.assertEqual(len(calendar.ends(day, "HOSE", "1d")), original_length)
        self.assertFalse(MarketCalendar((day,)).ends(day, "HOSE", "1d"))

    def test_financial_csv_read_once_per_file_revision_and_errors_stay_per_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "financial.csv"
            def write(eps):
                previous = path.stat().st_mtime_ns if path.exists() else 0
                path.write_text("symbol,period,published_on,eps\nFPT,2026-Q2,2026-08-01," + str(eps)
                                + "\nHPG,2026-Q2,2026-08-01,nan\n", encoding="utf-8")
                os.utime(path, ns=(max(previous + 1, path.stat().st_mtime_ns),) * 2)
            write(1000)
            provider = CsvDataProvider(Path(directory) / "unused.csv", path)
            with patch("fintech_bot.data.csv_provider.csv.DictReader", wraps=csv.DictReader) as read:
                self.assertEqual(provider.get_financials("FPT").eps, 1000)
                with self.assertRaisesRegex(ValueError, "hữu hạn"):
                    provider.get_financials("HPG")
                self.assertEqual(provider.get_financials("FPT").eps, 1000)
                self.assertEqual(read.call_count, 1)
                write(1200)
                self.assertEqual(provider.get_financials("FPT").eps, 1200)
                self.assertEqual(read.call_count, 2)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                provider.get_financials("FPT")


if __name__ == "__main__":
    unittest.main()
