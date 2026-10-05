import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fintech_bot.app import build_application
from fintech_bot.config import Settings, StrategySettings, PROJECT_ROOT
from fintech_bot.data.csv_provider import CsvDataProvider
from fintech_bot.data.universe import load_universe, select_universe
from fintech_bot.domain import Candle, Signal, SignalSide, VIETNAM
from fintech_bot.market import MarketCalendar, ReplayClock
from fintech_bot.services.backtest import BacktestSettings, run_backtest, write_backtest
from tests.test_project import RecordingNotifier


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=VIETNAM)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = MarketCalendar((date(2026, 9, 2),))

    def test_scheduler_excludes_weekends_holidays_and_lunch(self):
        for text in ("2026-09-19T10:00", "2026-09-02T10:00", "2026-09-18T12:00", "2026-09-18T08:59"):
            self.assertFalse(self.calendar.is_scan_time(instant(text)))
        self.assertTrue(self.calendar.is_scan_time(instant("2026-09-18T10:00")))
        self.assertTrue(self.calendar.is_scan_time(instant("2026-09-18T15:00:30")))

    def test_daily_has_only_session_close(self):
        self.assertEqual(self.calendar.ends(date(2026, 9, 18), "HOSE", "1d"), [instant("2026-09-18T14:45")])
        self.assertEqual(self.calendar.ends(date(2026, 9, 18), "UPCOM", "1d"), [instant("2026-09-18T15:00")])

    def test_latest_bar_on_monday_before_open_is_friday(self):
        end = self.calendar.latest_end(instant("2026-09-21T08:00"), "HOSE", "1d")
        self.assertEqual(end, instant("2026-09-18T14:45"))
        self.assertEqual(self.calendar.latest_end(instant("2026-09-18T12:00"), "UPCOM", "1d"), instant("2026-09-17T15:00"))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = Settings(("FPT", "HPG", "VNM"), Path(self.folder.name) / "test.sqlite3", StrategySettings(), timeframe="1d", scan_interval_seconds=60)
        self.notifier = RecordingNotifier()
        self.app = build_application(self.settings, self.notifier)
        self.addCleanup(self.app.close)

    def test_preferences_are_per_user_and_persisted(self):
        self.app.commands.handle("/alerts all", "alice")
        self.app.commands.handle("/timeframe 1d", "alice")
        self.app.commands.handle("/subscribe FPT", "bob")
        self.assertEqual(self.app.repository.preference("alice"), ("all", "1d"))
        self.assertEqual(self.app.repository.preference("bob"), ("watchlist", "1d"))
        other = build_application(self.settings, RecordingNotifier())
        try:
            self.assertEqual(other.repository.preference("alice"), ("all", "1d"))
        finally:
            other.close()

    def test_all_watchlist_and_off_have_different_recipients(self):
        self.app.commands.handle("/alerts all", "alice")
        self.app.commands.handle("/subscribe FPT", "bob")
        self.app.commands.handle("/subscribe FPT", "carol")
        self.app.commands.handle("/alerts off", "carol")
        self.app.commands.handle("/scan")
        self.assertEqual([chat for chat, _ in self.notifier.messages].count("alice"), 2)
        self.assertEqual([chat for chat, _ in self.notifier.messages].count("bob"), 1)
        self.assertNotIn("carol", [chat for chat, _ in self.notifier.messages])

    def test_daily_signal_is_confirmed_and_five_minute_falls_back(self):
        self.app.commands.handle("/timeframe 1d")
        daily = self.app.commands.handle("/stock FPT")
        self.assertIn("ĐÃ XÁC NHẬN", daily)
        reply = self.app.commands.handle("/timeframe 5m")
        self.assertIn("đã ngừng hỗ trợ", reply)
        self.assertEqual(self.app.repository.preference("local-demo"), ("watchlist", "1d"))
        self.assertIn("ĐÃ XÁC NHẬN", self.app.commands.handle("/stock FPT"))

    def test_daily_signal_keys_include_the_bar_close(self):
        signal = self.app.scanner.run().signals[0]
        next_signal = replace(signal, closed_at=signal.closed_at + timedelta(minutes=5))
        self.assertNotEqual(signal.key, next_signal.key)
        self.assertTrue(all(item.timeframe == "1d" for item in self.app.scanner.run().signals))

    def test_step_is_disabled_in_daily_only_runtime(self):
        before = self.app.clock.now()
        reply = self.app.commands.handle("/step")
        self.assertEqual(self.app.clock.now(), before)
        self.assertIn("đã ngừng hỗ trợ", reply)

    def test_candles_upsert_and_financials_are_persisted(self):
        self.app.scanner.run()
        count = self.app.repository.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        self.app.scanner.run()
        self.assertEqual(self.app.repository.connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0], count)
        self.assertEqual(self.app.repository.connection.execute("SELECT COUNT(*) FROM financials").fetchone()[0], 0)
        saved = self.app.repository.load_candles(self.app.scanner.provider.source, "FPT", "1d")
        self.assertEqual(saved, self.app.scanner.provider.get_candles("FPT", "1d"))

    def test_queue_limits_sends_and_preserves_remaining_messages(self):
        self.app.alerts.settings = replace(self.settings, max_alerts_per_cycle=1)
        self.app.commands.handle("/alerts all", "alice")
        self.app.commands.handle("/scan")
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.app.repository.pending_count(), 1)
        self.app.alerts.flush()
        self.assertEqual(len(self.notifier.messages), 1, "Repeated flush must not bypass the send budget")
        self.app.clock.advance(self.settings.scan_interval_seconds)
        self.app.alerts.flush()
        self.assertEqual(len(self.notifier.messages), 2)

    def test_queue_cancels_messages_after_opt_out(self):
        self.app.alerts.settings = replace(self.settings, max_alerts_per_cycle=1)
        self.app.commands.handle("/alerts all", "alice")
        self.app.commands.handle("/scan")
        self.app.commands.handle("/alerts off", "alice")
        self.app.clock.advance(self.settings.scan_interval_seconds)
        self.app.alerts.flush()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.app.repository.pending_count(), 0)

    def test_queue_expires_and_does_not_reenqueue_old_events(self):
        self.app.commands.handle("/subscribe FPT", "alice")
        report = self.app.scanner.run()
        self.notifier.fail = True
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            self.app.alerts.dispatch(report.signals)
        self.app.clock.advance(86401)
        self.notifier.fail = False
        self.app.alerts.dispatch(report.signals)
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(self.app.repository.pending_count(), 0)

    def test_data_failure_blocks_queued_alert_until_source_recovers(self):
        self.app.commands.handle("/subscribe FPT", "alice")
        self.notifier.fail = True
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            self.app.alerts.dispatch(self.app.scanner.run().signals)
        provider = self.app.scanner.provider
        original = provider.get_candles
        def unavailable(symbol, timeframe):
            raise OSError("Source offline")
        provider.get_candles = unavailable
        self.app.clock.advance(5)
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            self.app.scanner.run(["FPT"])
        self.notifier.fail = False
        self.app.alerts.flush()
        self.assertEqual(self.notifier.messages, [])
        provider.get_candles = original
        self.app.alerts.dispatch(self.app.scanner.run(["FPT"]).signals)
        self.assertEqual(len(self.notifier.messages), 1)

    def test_new_signal_invalidates_old_queued_event(self):
        self.app.commands.handle("/subscribe FPT", "alice")
        original = next(signal for signal in self.app.scanner.run().signals if signal.symbol == "FPT" and signal.timeframe == "1d")
        self.notifier.fail = True
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            self.app.alerts.dispatch([original])
        replacement = replace(original, side=SignalSide.NONE)
        self.app.repository.save_signal(replacement)
        self.app.clock.advance(5)
        self.notifier.fail = False
        self.app.alerts.dispatch([replacement])
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(self.app.repository.pending_count(), 0)

    def test_scheduler_obeys_intervals_and_session_boundaries(self):
        first = self.app.scheduler.tick()
        self.assertTrue(first.scanned)
        self.assertFalse(self.app.scheduler.tick().scanned)
        self.app.clock.advance(60)
        self.assertTrue(self.app.scheduler.tick().scanned)
        self.app.clock.current = instant("2026-09-18T12:00")
        self.assertFalse(self.app.scheduler.tick().scanned)

    def test_stale_data_cannot_generate_fresh_alert(self):
        real_provider = self.app.scanner.provider
        fixed = {"1d": real_provider.get_candles("FPT", "1d")}
        class FrozenProvider:
            source = "frozen"
            is_demo = True
            def get_candles(self, symbol, timeframe):
                return fixed[timeframe]
            def get_financials(self, symbol):
                return None
        self.app.scanner.provider = FrozenProvider()
        self.app.clock.current = instant("2026-09-21T15:01")
        self.app.commands.handle("/subscribe FPT")
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            result = self.app.scanner.run(["FPT"])
        self.assertIn("Dữ liệu cũ", result.errors["FPT/1d"])
        self.assertEqual(result.signals, [])

    def test_scanner_rejects_five_minute_runtime_request(self):
        provider = self.app.scanner.provider
        with patch.object(provider, "get_candles", wraps=provider.get_candles) as fetch:
            with self.assertRaisesRegex(ValueError, "chỉ hỗ trợ"):
                self.app.scanner.run(["FPT"], ("5m",))
        fetch.assert_not_called()

    def test_csv_reads_roundtrip_and_rejects_naive_timestamps(self):
        path = Path(self.folder.name) / "prices.csv"
        candle = self.app.scanner.provider.get_candles("FPT", "1d")[-1]
        fields = ["symbol", "timeframe", "closed_at", "open", "high", "low", "close", "volume", "is_closed"]
        def write(timestamp):
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerow(dict(symbol="FPT", timeframe="1d", closed_at=timestamp, open=candle.open,
                                     high=candle.high, low=candle.low, close=candle.close, volume=candle.volume, is_closed="true"))
        write(candle.timestamp.isoformat())
        self.assertEqual(CsvDataProvider(path).get_candles("FPT", "1d")[0].close, candle.close)
        write("2026-09-18T10:00:00")
        with self.assertRaises(ValueError):
            CsvDataProvider(path).get_candles("FPT", "1d")

    def test_universe_covers_three_exchanges_and_excludes_funds(self):
        universe = load_universe(PROJECT_ROOT / "fixtures" / "universe.csv")
        selected = select_universe(universe, ("HOSE", "HNX", "UPCOM"))
        self.assertEqual({item.exchange for item in selected}, {"HOSE", "HNX", "UPCOM"})
        self.assertNotIn("ETFDEMO", [item.symbol for item in selected])
        self.assertIn("PAUSED01", [item.symbol for item in selected])


class BacktestTests(unittest.TestCase):
    def test_orders_execute_only_on_next_bar_and_include_fees(self):
        candles = [Candle("AAA", date(2026, 1, i+1), 100, 111, 99, 110, 1000) for i in range(6)]
        class Scripted:
            required_bars = 2
            def evaluate(self, symbol, values, **kwargs):
                side = {2: SignalSide.BUY, 4: SignalSide.SELL}.get(len(values), SignalSide.NONE)
                return Signal(symbol, values[-1].session, side, "test", "", {}, "test", True)
        result = run_backtest("AAA", candles, Scripted(), source="test", is_demo=True,
                              settings=BacktestSettings(initial_cash=1000, fee_rate=0.01,
                                                        slippage_rate=0, lot_size=1, min_holding_sessions=0))
        self.assertEqual([trade["quantity"] for trade in result["trades"]], [9, 9])
        self.assertTrue(result["trades"][0]["executed_at"].startswith("2026-01-03"))
        self.assertEqual(result["final_equity"], 982)
        self.assertEqual(result["closed_trades"], 1)

    def test_last_bar_signal_never_creates_an_impossible_fill(self):
        candles = [Candle("AAA", date(2026, 1, i+1), 100, 101, 99, 100, 100) for i in range(3)]
        class LastBar:
            required_bars = 2
            def evaluate(self, symbol, values, **kwargs):
                return Signal(symbol, values[-1].session, SignalSide.BUY if len(values) == 3 else SignalSide.NONE,
                              "test", "", {}, "test", True)
        result = run_backtest("AAA", candles, LastBar(), source="test", is_demo=True)
        self.assertEqual(result["trades"], [])
        self.assertIsNone(result["win_rate_percent"])
        with tempfile.TemporaryDirectory() as folder:
            md, output = write_backtest(result, Path(folder))
            self.assertIn("KHÔNG PHẢI", md.read_text(encoding="utf-8"))
            self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["is_demo"])


if __name__ == "__main__":
    unittest.main()
