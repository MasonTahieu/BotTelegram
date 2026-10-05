"""Regression tests for readiness, priority repair, and safe public output."""

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fintech_bot.app import build_application
from fintech_bot.bot.formatting import format_signal
from fintech_bot.bot.telegram import TelegramClient, TelegramError
from fintech_bot.config import Settings, StrategySettings
from fintech_bot.data.readiness import ComponentHealth, DataNotReady, ReadinessState
from fintech_bot.data.vietcap import VietcapCacheProvider
from fintech_bot.data.vietcap_http import VietcapClient, SourceUnavailable, atomic_json, read_json
from fintech_bot.domain import Instrument, Signal, SignalSide, VIETNAM
from fintech_bot.market import MarketCalendar, ReplayClock
from fintech_bot.services.backtest import write_backtest
from fintech_bot.services.live_data import BACKGROUND_PRIORITY, LiveData, USER_PRIORITY
from tests.test_data_refresh import BatchProvider
from tests.test_project import RecordingNotifier
from tests.test_release import quote
from tests.test_vietcap import NOW, financials, prices, universe


TOKEN = "123456:" + "a" * 30


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calendar = MarketCalendar()
        self.settings = Settings(("FPT",), self.root / "app.db", StrategySettings(),
                                 mode="vietcap", vietcap_path=self.root)

    def test_health_snapshot_has_no_minute_component(self):
        settings = Settings(("FPT", "HPG"), self.root / "db.sqlite3", StrategySettings(),
                            mode="vietcap", vietcap_path=self.root)
        live = LiveData(settings, [Instrument("FPT", "HOSE"), Instrument("HPG", "HOSE")],
                        MarketCalendar())
        snapshot = live.status_snapshot()
        self.assertIn("live_quote", snapshot)
        self.assertIn("financials", snapshot)
        self.assertNotIn("minute", snapshot)

    def test_missing_cache_uses_typed_error_without_local_path(self):
        atomic_json(self.root / "universe.json", universe())
        provider = VietcapCacheProvider(self.root, self.calendar)
        with self.assertRaises(DataNotReady) as failure:
            provider.get_historical_candles("FPT", "1d")
        self.assertNotIn(str(self.root), str(failure.exception))
        self.assertNotIn("WinError", str(failure.exception))

    def test_priority_keeps_every_component_and_promotes_duplicates(self):
        live = LiveData(self.settings, [Instrument("FPT", "HOSE")], self.calendar)
        live.request_components("FPT", ("quote",), BACKGROUND_PRIORITY)
        live.request_symbol("FPT", "1d")
        live.request_components("FPT", ("ratios", "income", "quote"), USER_PRIORITY)
        self.assertEqual(set(live.priority), {
            ("FPT", "ratios"), ("FPT", "income"), ("FPT", "quote"),
        })
        self.assertEqual(live.priority["FPT", "quote"][0], USER_PRIORITY)

    def test_corrupt_quote_is_invalid_then_atomic_repair_clears_failure(self):
        (self.root / "FPT-quote.json").write_text("{broken", encoding="utf-8")
        failures = {"FPT-quote": {"at": "2020-01-01T00:00:00+07:00", "error": "old"}}
        atomic_json(self.root / "failures.json", failures)
        live = LiveData(self.settings, [Instrument("FPT", "HOSE")], self.calendar)
        self.assertEqual(live.component_health("FPT", "quote").state, ReadinessState.INVALID)

        class Client:
            def quotes(self, symbols):
                sample = quote()
                return {**sample, "data": [sample["data"]]}

        live.client = Client()
        live._component("FPT", "quote", failures)
        self.assertNotIn("FPT-quote", read_json(self.root / "failures.json"))
        self.assertTrue((self.root / "FPT-quote.json").exists())

    def test_old_quote_is_stale(self):
        live = LiveData(self.settings, [Instrument("FPT", "HOSE")], self.calendar)
        atomic_json(self.root / "FPT-quote.json", quote())
        self.assertEqual(live.component_health("FPT", "quote").state, ReadinessState.STALE)

    def test_financial_ensure_fetches_both_files_and_becomes_ready(self):
        live = LiveData(self.settings, [Instrument("FPT", "HOSE")], self.calendar)
        ratios, income = financials()
        calls = []
        now = datetime.now(VIETNAM)
        # This test models a fresh download, independently of the fixture's date.
        ratios["fetched_at"] = income["fetched_at"] = now.isoformat()
        stamps = [item.replace(hour=7, minute=0)
                  for item in self.calendar.history_ends(now, "HOSE", "1d", 60)]

        class Client:
            def financials(self, symbol, component):
                calls.append((symbol, component))
                return ratios if component == "ratios" else income
            def candles(self, symbol, timeframe, count):
                return prices(symbol, stamps=stamps, now=now)

        live.client = Client()
        def synchronous_tick():
            live._drain_priority({}, live._take_priority())
        with patch.object(live, "tick", side_effect=synchronous_tick):
            health = live.ensure_components("FPT", ("ratios", "income"), timeout=2)
        self.assertTrue(all(item.ready for item in health.values()))
        self.assertEqual(calls, [("FPT", "ratios"), ("FPT", "income")])
        self.assertTrue((self.root / "FPT-ratios.json").exists())
        self.assertTrue((self.root / "FPT-income.json").exists())

    def test_one_component_failure_does_not_stop_the_next_symbol(self):
        settings = Settings(("FPT", "HPG"), self.root / "two.db", StrategySettings(),
                            mode="vietcap", vietcap_path=self.root)
        live = LiveData(settings, [Instrument("FPT", "HOSE"), Instrument("HPG", "HOSE")], self.calendar)
        now = datetime.now(VIETNAM)
        stamps = [item.replace(hour=7, minute=0)
                  for item in self.calendar.history_ends(now, "HOSE", "1d", 60)]

        class Client:
            def financials(self, symbol, component):
                if symbol == "FPT":
                    raise TimeoutError("timed out")
                return financials("HPG")[0]

        live.client = Client()
        failures = {}
        live._drain_priority(failures, [("FPT", "ratios"), ("HPG", "ratios")])
        self.assertIn("FPT-ratios", failures)
        self.assertEqual(failures["FPT-ratios"]["category"], "timeout")
        self.assertEqual(failures["FPT-ratios"]["attempts"], 1)
        self.assertIn("retry_at", failures["FPT-ratios"])
        self.assertTrue((self.root / "HPG-ratios.json").exists())
        self.assertNotIn("HPG-ratios", failures)


class ScannerAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.symbols = ("FPT", "HPG")
        self.clock = ReplayClock(datetime(2026, 9, 18, 10, tzinfo=VIETNAM))
        self.settings = Settings(self.symbols, self.root / "app.db", StrategySettings(),
                                 timeframe="1d", scan_interval_seconds=60)
        self.app = build_application(self.settings, RecordingNotifier(), self.clock)
        self.addCleanup(self.app.close)

    def test_failed_daily_frame_is_not_cached_and_can_retry(self):
        provider = BatchProvider(self.app.scanner.provider, self.symbols)
        self.app.scanner.provider = provider
        self.app.scanner.run(["FPT"])
        provider.errors["FPT", "1d"] = "timeout"
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            self.app.scanner.run(["FPT"])
        self.assertNotIn(("FPT", "1d"), self.app.scanner.last_checked)
        provider.errors.clear()
        self.assertIsNotNone(self.app.scanner.refresh_if_needed("FPT", "1d"))

    def test_missing_quote_does_not_remove_daily_signal(self):
        self.app.scanner.run()
        self.app.commands.handle("/timeframe 1d", "alice")

        class PendingLive:
            @staticmethod
            def request_symbol(symbol, timeframe): pass
            @staticmethod
            def quote_summary(symbol): return "Bảng giá hiện tại chưa sẵn sàng."
            @staticmethod
            def request_refresh(): pass

        self.app.commands.live_data = PendingLive()
        output = self.app.commands.handle("/stock FPT", "alice")
        self.assertIn("Bảng giá hiện tại chưa sẵn sàng", output)
        self.assertTrue(any(item.symbol == "FPT" and item.timeframe == "1d"
                            for item in self.app.scanner.latest_report.signals))
        self.assertNotIn("FPT/1d", self.app.scanner.latest_report.errors)

    def test_financial_command_requests_both_components(self):
        calls = []

        class PendingLive:
            @staticmethod
            def ensure_components(symbol, components):
                calls.append((symbol, components))
                return {name: ComponentHealth(ReadinessState.MISSING) for name in components}
            @staticmethod
            def readiness_message(symbol, health): return "đang cập nhật"

        self.app.commands.live_data = PendingLive()
        self.assertEqual(self.app.commands.handle("/financials FPT"), "đang cập nhật")
        self.assertEqual(calls, [("FPT", ("ratios", "income"))])

    def test_ready_stock_request_scans_only_selected_timeframe(self):
        self.app.commands.handle("/timeframe 1d", "alice")

        class ReadyLive:
            @staticmethod
            def request_symbol(symbol, timeframe): pass
            @staticmethod
            def quote_summary(symbol): return ""

        self.app.commands.live_data = ReadyLive()
        with patch.object(self.app.scanner, "run", wraps=self.app.scanner.run) as scan:
            self.app.commands.handle("/stock FPT", "alice")
        scan.assert_called_once_with(["FPT"], ("1d",))

    def test_missing_daily_stock_request_does_not_bootstrap_in_live_runtime(self):
        fixed = datetime(2026, 9, 18, 16, tzinfo=VIETNAM)
        root = self.root / "live"
        root.mkdir()
        atomic_json(root / "universe.json", universe())
        settings = Settings(("FPT",), root / "app.db", StrategySettings(), mode="vietcap",
                            timeframe="1d", vietcap_path=root)
        app = build_application(settings, RecordingNotifier(), ReplayClock(fixed))
        self.addCleanup(app.close)
        live = LiveData(settings, app.scanner.instruments.values(), app.scanner.calendar)
        stamps = [item.replace(hour=7, minute=0)
                  for item in app.scanner.calendar.history_ends(fixed, "HOSE", "1d", 60)]
        daily = prices("FPT", stamps=stamps, now=fixed)
        current = quote(observed=fixed.isoformat(), source="2026-09-18T14:45:00+07:00")

        class Client:
            def candles(self, symbol, timeframe, count): return daily
            def quotes(self, symbols): return {**current, "data": [current["data"]]}

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return fixed if tz else fixed.replace(tzinfo=None)

        live.client = Client()
        app.commands.live_data = live
        def synchronous_tick():
            live._drain_priority({}, live._take_priority())
        with patch("fintech_bot.services.live_data.datetime", FixedDateTime), \
                patch.object(live, "tick", side_effect=synchronous_tick):
            output = app.commands.handle("/stock FPT", "alice")
        self.assertIn("Historical ERROR", output)
        self.assertNotIn(("FPT", "1d"), app.scanner.last_checked)
        self.assertFalse((root / "FPT-daily.json").exists())
        self.assertFalse((root / "FPT-quote.json").exists())

    def test_financial_filter_does_not_block_sell(self):
        self.app.alerts.register("alice")
        self.app.repository.set_financial_filter("alice", {"roe_min": 1_000_000})
        report = self.app.scanner.run()
        buy = next(item for item in report.signals if item.symbol == "FPT" and item.side == SignalSide.BUY)
        sell = next(item for item in report.signals if item.symbol == "HPG" and item.side == SignalSide.SELL)
        self.assertFalse(self.app.alerts.financially_eligible("alice", buy))
        self.assertTrue(self.app.alerts.financially_eligible("alice", sell))


class PublicOutputTests(unittest.TestCase):
    def test_vietcap_429_circuit_reopens_after_retry_after(self):
        clock = [0.0]
        calls = []
        def sleep(seconds): clock[0] += seconds
        def opener(request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "2"}, None)
            return io.BytesIO(json.dumps({"ok": True}).encode())
        client = VietcapClient(interval=.5, opener=opener, sleep=sleep, monotonic=lambda: clock[0])
        with self.assertRaises(SourceUnavailable) as first:
            client.universe()
        self.assertEqual(first.exception.retry_after, 2)
        with self.assertRaises(SourceUnavailable):
            client.universe()
        self.assertEqual(len(calls), 1)
        clock[0] = 3
        self.assertEqual(client.universe()["data"], {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_status_hides_vietcap_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(root / "universe.json", universe())
            settings = Settings((), root / "app.db", StrategySettings(), mode="vietcap", vietcap_path=root)
            app = build_application(settings)
            try:
                status = app.commands.handle("/status")
            finally:
                app.close()
        self.assertIn("Nguồn dữ liệu: Vietcap", status)
        self.assertIn("SKIPPED reasons:", status)
        self.assertIn("no_recent_trade: 0", status)
        self.assertNotIn(str(root), status)

    def test_signal_and_backtest_hide_internal_vietcap_path(self):
        source = r"vietcap-public:v1:E:\private\cache"
        signal = Signal("FPT", NOW.date(), SignalSide.BUY, "strategy", "reason", {}, source, False,
                        NOW, "1d", True, "HOSE")
        rendered = format_signal(signal)
        self.assertIn("Nguồn: Vietcap", rendered)
        self.assertNotIn("private", rendered)
        result = {"symbol": "FPT", "timeframe": "1d", "is_demo": False, "source": source,
                  "first_evaluation": NOW.isoformat(), "last_bar": NOW.isoformat(), "bars": 60,
                  "closed_trades": 0, "return_percent": 0, "max_drawdown_percent": 0,
                  "win_rate_percent": None, "assumptions": {}, "limitations": []}
        with tempfile.TemporaryDirectory() as folder:
            report, _ = write_backtest(result, Path(folder))
            text = report.read_text(encoding="utf-8")
        self.assertIn("VIETCAP", text)
        self.assertNotIn("CSV local", text)
        self.assertNotIn("private", text)

    def test_telegram_error_keeps_safe_structured_diagnostics(self):
        def conflict(request, **kwargs):
            return io.BytesIO(json.dumps({"ok": False, "error_code": 409,
                                          "description": "terminated by other getUpdates request"}).encode())

        with self.assertRaises(TelegramError) as failure:
            TelegramClient(TOKEN, opener=conflict).call("getUpdates")
        error = failure.exception
        self.assertEqual(error.status, 409)
        self.assertEqual(error.error_code, 409)
        self.assertIn("other getUpdates", error.description)
        self.assertIn("409 Conflict", str(error))


if __name__ == "__main__":
    unittest.main()
