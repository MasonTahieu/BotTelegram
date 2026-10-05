import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fintech_bot.app import build_application
from fintech_bot.bot.telegram import IncrementalScanner
from fintech_bot.config import Settings, StrategySettings
from fintech_bot.data.vietcap_sync import _collect
from fintech_bot.domain import Instrument, VIETNAM
from fintech_bot.market import MarketCalendar
from fintech_bot.services.live_data import COMPONENTS, LiveData


class DailyOnlyRuntimeTests(unittest.TestCase):
    def test_live_refresh_never_requests_minute_or_intraday(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = Settings(("FPT",), root / "app.db", StrategySettings(),
                                mode="vietcap", vietcap_path=root)
            calendar = MarketCalendar()
            class Client:
                pass

            live = LiveData(settings, [Instrument("FPT", "HOSE")], calendar, client=Client())
            self.assertEqual(live.components_for_timeframe("1d"), ("quote",))
            self.assertNotIn("daily", COMPONENTS)
            self.assertNotIn("minute", COMPONENTS)
            with self.assertRaisesRegex(ValueError, "Component"):
                live.request_components("FPT", ("daily",))
            with self.assertRaisesRegex(ValueError, "chỉ hỗ trợ"):
                live.components_for_timeframe("5m")

    def test_incremental_scan_uses_small_batches(self):
        symbols = tuple(f"S{index:03}" for index in range(58))

        class Scanner:
            def __init__(self):
                self.symbols = symbols
                self.calls = []

            def run(self, selected):
                self.calls.append(tuple(selected))
                return SimpleNamespace(signals=[])

        class Alerts:
            def dispatch(self, signals):
                return None

        app = SimpleNamespace(scanner=Scanner(), alerts=Alerts())
        incremental = IncrementalScanner(app, chunk_size=25)
        incremental.schedule(symbols)
        while incremental.active:
            incremental.step()
        self.assertEqual([len(item) for item in app.scanner.calls], [25, 25, 8])

    def test_old_five_minute_config_and_user_fall_back_without_losing_subscription(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(("FPT",), Path(folder) / "app.db", StrategySettings(), timeframe="5m")
            self.assertEqual(settings.timeframe, "1d")
            app = build_application(settings)
            try:
                app.commands.handle("/subscribe FPT", "alice")
                reply = app.commands.handle("/timeframe 5m", "alice")
                self.assertIn("đã ngừng hỗ trợ", reply)
                self.assertEqual(app.repository.preference("alice"), ("watchlist", "1d"))
                self.assertEqual(app.repository.subscriptions("alice"), ["FPT"])
                status = app.commands.handle("/status", "alice")
                self.assertNotIn("MINUTE", status.upper())
                self.assertNotIn("5 phút", status)
            finally:
                app.close()

    def test_explicit_collector_rejects_minute_before_network(self):
        class Client:
            interval = 0
            requests = 0

            def universe(self):
                raise AssertionError("Invalid minute request reached the network")

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "minute đã ngừng"):
                _collect(folder, components=("minute",), client=Client(), progress=lambda _: None)


if __name__ == "__main__":
    unittest.main()
