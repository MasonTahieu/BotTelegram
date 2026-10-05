import csv
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from fintech_bot.app import build_application
from fintech_bot.config import PROJECT_ROOT, Settings, StrategySettings, load_settings
from fintech_bot.data.validation import DataValidationError, validate_candles
from fintech_bot.domain import VIETNAM
from tests.test_project import RecordingNotifier


class LocalHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(("FPT",), self.root / "test.sqlite3", StrategySettings(), timeframe="1d", scan_interval_seconds=60)
        self.notifier = RecordingNotifier()
        self.app = build_application(self.settings, self.notifier)
        self.addCleanup(self.app.close)

    def alter_last_candle(self, timeframe, transform):
        provider = self.app.scanner.provider
        original = provider.get_candles
        def altered(symbol, requested_timeframe):
            candles = original(symbol, requested_timeframe)
            if requested_timeframe == timeframe:
                candles[-1] = transform(candles[-1])
            return candles
        provider.get_candles = altered

    def test_step_is_disabled_without_changing_fractional_clock(self):
        self.app.clock.advance(0.123456)
        result = self.app.commands.handle("/step")
        expected = datetime(2026, 9, 18, 10, 0, 0, 123456, tzinfo=VIETNAM)
        self.assertEqual(self.app.clock.now(), expected)
        self.assertIn("đã ngừng hỗ trợ", result)

    def test_intraday_frame_is_not_used_by_scanner(self):
        report = self.app.scanner.run()
        self.assertTrue(report.signals)
        self.assertTrue(all(item.timeframe == "1d" for item in report.signals))

    def test_daily_bar_cannot_claim_to_close_during_morning(self):
        self.alter_last_candle("1d", lambda item: replace(item, closed_at=self.app.clock.now(), is_closed=True))
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            report = self.app.scanner.run()
        self.assertIn("không khớp", report.errors["FPT/1d"])

    def test_preview_cannot_use_wrong_exchange_close(self):
        self.alter_last_candle("1d", lambda item: replace(item, closed_at=item.timestamp.replace(hour=16)))
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            report = self.app.scanner.run()
        self.assertIn("không thuộc lịch", report.errors["FPT/1d"])

    def test_weekend_candle_is_not_treated_as_new_data(self):
        self.app.clock.current = datetime(2026, 9, 19, 10, tzinfo=VIETNAM)
        self.alter_last_candle("1d", lambda item: replace(item, session=self.app.clock.now().date(),
                               closed_at=self.app.clock.now(), observed_at=self.app.clock.now()))
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            report = self.app.scanner.run()
        self.assertIn("không", report.errors["FPT/1d"])

    def test_invalid_candle_status_and_observation_are_rejected(self):
        candle = self.app.scanner.provider.get_candles("FPT", "1d")[-1]
        invalid = [
            replace(candle, closed_at=candle.timestamp + timedelta(days=1)),
            replace(candle, is_closed="false"),
            replace(candle, observed_at=candle.timestamp.replace(tzinfo=None)),
            replace(candle, is_closed=False, observed_at=None),
            replace(candle, is_closed=False, observed_at=candle.timestamp),
            replace(candle, is_closed=False, observed_at=candle.timestamp - timedelta(days=1)),
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(DataValidationError):
                validate_candles("FPT", [item], 1)

    def test_opt_back_in_can_receive_unsent_unexpired_alert(self):
        self.app.commands.handle("/subscribe FPT", "alice")
        self.notifier.fail = True
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            self.app.commands.handle("/scan")
        self.app.commands.handle("/alerts off", "alice")
        self.app.clock.advance(5)
        self.app.alerts.flush()
        row = self.app.repository.connection.execute("SELECT attempts,status FROM outbox WHERE chat_id='alice'").fetchone()
        self.assertEqual(tuple(row), (1, "cancelled"))
        self.notifier.fail = False
        self.app.commands.handle("/alerts watchlist", "alice")
        self.app.commands.handle("/scan")
        self.assertEqual(len(self.notifier.messages), 1)
        self.app.commands.handle("/alerts off", "alice")
        self.app.commands.handle("/alerts watchlist", "alice")
        self.app.commands.handle("/scan")
        self.assertEqual(len(self.notifier.messages), 1)

    def test_retry_error_is_reported_between_scheduled_scans(self):
        self.app.commands.handle("/subscribe FPT", "alice")
        self.notifier.fail = True
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            self.app.scheduler.tick()
        self.app.clock.advance(5)
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            result = self.app.scheduler.tick()
        self.assertFalse(result.scanned)
        self.assertEqual(result.errors, 1)

    def test_full_csv_pipeline_matches_the_replay_snapshot(self):
        csv_path = self.root / "prices.csv"
        columns = ("symbol", "timeframe", "closed_at", "open", "high", "low", "close", "volume", "is_closed", "observed_at")
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for timeframe in ("1d",):
                for item in self.app.scanner.provider.get_candles("FPT", timeframe):
                    writer.writerow({"symbol": item.symbol, "timeframe": item.timeframe,
                        "closed_at": item.timestamp.isoformat(), "open": item.open, "high": item.high,
                        "low": item.low, "close": item.close, "volume": item.volume,
                        "is_closed": str(item.is_closed).lower(), "observed_at": item.observed_at.isoformat()})
        universe = self.root / "universe.csv"
        universe.write_text("symbol,exchange,name,asset_type,status\nFPT,HOSE,CSV test,stock,active\n", encoding="utf-8")
        settings = replace(self.settings, mode="csv", database_path=self.root / "csv.sqlite3",
                           universe_path=universe, candles_path=csv_path)
        app = build_application(settings, RecordingNotifier(), clock=self.app.clock)
        try:
            report = app.scanner.run()
            self.assertFalse(report.errors)
            self.assertEqual(len(report.signals), 1)
            self.assertIn("FPT: MUA", app.commands.handle("/stock FPT"))
            self.assertTrue(all(not signal.is_demo for signal in report.signals))
        finally:
            app.close()


class ConfigAndProcessTests(unittest.TestCase):
    def test_invalid_table_structure_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for name in ("runtime", "strategy", "data", "alerts"):
                path.write_text(f'symbols=["FPT"]\n{name}="mistyped"\n', encoding="utf-8")
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "phải là một bảng"):
                    load_settings(path)

    def test_bounded_watch_returns_failure_when_scans_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('symbols=["FPT"]\n[strategy]\nema_slow=200\n', encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "fintech_bot", "watch", "--cycles", "1", "--fast",
                                     "--config", str(config), "--db", str(root / "state.sqlite3")],
                                    cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("lỗi: 1", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_bounded_watch_still_returns_success_when_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "fintech_bot", "watch", "--cycles", "2", "--fast",
                                     "--db", str(Path(directory) / "state.sqlite3")],
                                    cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
