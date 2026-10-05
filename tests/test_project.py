import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fintech_bot.app import build_application
from fintech_bot.config import Settings, StrategySettings, load_settings
from fintech_bot.data.demo import DemoDataProvider
from fintech_bot.data.validation import DataValidationError, validate_candles
from fintech_bot.domain import SignalSide
from fintech_bot.storage.sqlite import SqliteRepository
from fintech_bot.strategies.ema_rsi import EmaRsiStrategy
from fintech_bot.strategies.indicators import ema, rsi


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = False

    def send(self, chat_id: str, text: str) -> None:
        if self.fail:
            raise OSError("Simulated transport failure")
        self.messages.append((chat_id, text))


class StrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = DemoDataProvider(("FPT", "HPG", "VNM"))
        self.strategy = EmaRsiStrategy(StrategySettings())

    def test_ema_uses_sma_seed_and_recursive_update(self) -> None:
        self.assertEqual(ema([1, 2, 3, 4], 3), [None, None, 2, 3])

    def test_wilder_rsi_known_sequence_and_flat_market(self) -> None:
        result = rsi([1, 2, 3, 2, 2, 3], 2)
        self.assertEqual(result[:2], [None, None])
        self.assertEqual(result[2:5], [100, 50, 50])
        self.assertAlmostEqual(result[5], 83.3333333333)
        self.assertEqual(rsi([5.0] * 20, 14)[-1], 50)
        self.assertEqual(rsi([5, 4, 3, 2], 2)[-1], 0)

    def test_synthetic_scenarios_cover_buy_sell_and_none(self) -> None:
        for symbol, expected in (("FPT", SignalSide.BUY), ("HPG", SignalSide.SELL), ("VNM", SignalSide.NONE)):
            with self.subTest(symbol=symbol):
                signal = self.strategy.evaluate(
                    symbol, self.provider.get_candles(symbol), source="test", is_demo=True,
                )
                self.assertEqual(signal.side, expected)
                self.assertTrue(signal.is_demo)
                self.assertTrue(all(math.isfinite(value) for value in signal.indicators.values()))

    def test_existing_uptrend_is_not_a_new_crossover(self) -> None:
        candles = self.provider.get_candles("FPT")
        candles = [replace(candle, open=100 + i, high=101 + i, low=99 + i, close=100 + i)
                   for i, candle in enumerate(candles)]
        signal = self.strategy.evaluate("FPT", candles, source="test", is_demo=True)
        self.assertGreater(signal.indicators["ema_fast"], signal.indicators["ema_slow"])
        self.assertEqual(signal.side, SignalSide.NONE)

    def test_insufficient_history_is_rejected(self) -> None:
        with self.assertRaises(DataValidationError):
            self.strategy.evaluate("FPT", self.provider.get_candles("FPT")[:50], source="test", is_demo=True)

    def test_duplicate_dates_and_invalid_prices_are_rejected(self) -> None:
        original = self.provider.get_candles("FPT")
        invalid_endings = [
            replace(original[-1], session=original[-2].session),
            replace(original[-1], close=float("nan")),
            replace(original[-1], high=1),
            replace(original[-1], volume=-1),
        ]
        for ending in invalid_endings:
            with self.subTest(ending=ending), self.assertRaises(DataValidationError):
                validate_candles("FPT", original[:-1] + [ending], 51)


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(("FPT", "HPG", "VNM"), Path(self.directory.name) / "test.sqlite3", StrategySettings(), timeframe="5m", scan_interval_seconds=60)
        self.notifier = RecordingNotifier()
        self.app = build_application(self.settings, self.notifier)
        self.addCleanup(self.app.close)

    def test_commands_and_scan_work_without_api(self) -> None:
        self.assertIn("giả lập", self.app.commands.handle("/start"))
        # A user request now refreshes the cache when there has been no scan yet.
        self.assertIn("FPT: MUA", self.app.commands.handle("/signals"))
        self.assertIn("3/3", self.app.commands.handle("/scan"))
        output = self.app.commands.handle("/stock fpt")
        self.assertIn("FPT: MUA", output)
        self.assertIn("dữ liệu giả lập", output)
        self.assertIn("Phiên dữ liệu", output)
        self.assertIn("Lý do", output)

    def test_bad_commands_do_not_crash(self) -> None:
        for command in ("", "/invalid", "/stock", "/stock XYZ", "/stock FPT HPG", "/scan extra"):
            with self.subTest(command=command):
                self.assertTrue(self.app.commands.handle(command))
        self.assertIn("chưa có trong demo", self.app.commands.handle("/subscribe XYZ"))
        self.assertEqual(self.app.repository.subscriptions("local-demo"), [])

    def test_alerts_are_per_user_deduplicated_and_skip_none(self) -> None:
        for command in ("/subscribe FPT", "/subscribe HPG", "/subscribe VNM"):
            self.app.commands.handle(command, "alice")
        self.app.commands.handle("/subscribe FPT", "bob")
        self.app.commands.handle("/scan")
        self.assertEqual(len(self.notifier.messages), 3)
        self.app.commands.handle("/scan")
        self.assertEqual(len(self.notifier.messages), 3)
        self.assertEqual(sum(chat == "alice" for chat, _ in self.notifier.messages), 2)
        self.assertEqual(sum(chat == "bob" for chat, _ in self.notifier.messages), 1)

    def test_deliveries_and_subscriptions_survive_restart(self) -> None:
        self.app.commands.handle("/subscribe FPT", "alice")
        self.app.commands.handle("/scan")
        second_notifier = RecordingNotifier()
        second_app = build_application(self.settings, second_notifier)
        try:
            self.assertEqual(second_app.repository.subscriptions("alice"), ["FPT"])
            second_app.commands.handle("/scan")
            self.assertEqual(second_notifier.messages, [])
        finally:
            second_app.close()

    def test_failed_delivery_is_retried_on_next_scan(self) -> None:
        self.app.commands.handle("/subscribe FPT", "alice")
        self.notifier.fail = True
        with self.assertLogs("fintech_bot.services.alerts", level="ERROR"):
            self.assertIn("Không gửi được", self.app.commands.handle("/scan"))
        self.assertEqual(self.notifier.messages, [])
        self.notifier.fail = False
        self.app.clock.advance(self.settings.retry_seconds)
        self.app.commands.handle("/scan")
        self.assertEqual(len(self.notifier.messages), 1)

    def test_unsubscribe_only_removes_target_user(self) -> None:
        self.app.commands.handle("/subscribe FPT", "alice")
        self.app.commands.handle("/subscribe FPT", "bob")
        self.app.commands.handle("/unsubscribe FPT", "alice")
        self.app.commands.handle("/scan")
        self.assertEqual([chat for chat, _ in self.notifier.messages], ["bob"])

    def test_failure_for_one_symbol_does_not_return_stale_result(self) -> None:
        self.app.commands.handle("/scan")
        original_provider = self.app.scanner.provider

        class PartialFailureProvider:
            source = original_provider.source
            is_demo = True

            def get_candles(self, symbol, timeframe="1d"):
                if symbol == "FPT":
                    raise OSError("Nguồn dữ liệu tạm ngừng")
                return original_provider.get_candles(symbol, timeframe)

            def get_financials(self, symbol):
                return None

        self.app.scanner.provider = PartialFailureProvider()
        with self.assertLogs("fintech_bot.services.scanner", level="ERROR"):
            output = self.app.commands.handle("/scan")
        self.assertIn("2/3", output)
        self.assertIn("bị lỗi", self.app.commands.handle("/stock FPT"))
        self.assertNotIn("FPT: MUA", self.app.commands.handle("/signals"))
        self.assertIn("HPG: BÁN", self.app.commands.handle("/signals"))

    def test_signal_round_trip_and_strategy_isolation(self) -> None:
        signal = self.app.scanner.run().signals[0]
        found = self.app.repository.latest_signal(signal.symbol, signal.strategy_id, signal.source)
        self.assertEqual(found, signal)
        changed_settings = replace(self.settings.strategy, buy_rsi_min=55)
        self.assertNotEqual(signal.strategy_id, changed_settings.strategy_id)
        self.assertIsNone(self.app.repository.latest_signal(signal.symbol, changed_settings.strategy_id, signal.source))


class ConfigTests(unittest.TestCase):
    def test_default_config_is_demo_only(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.mode, "demo")
        self.assertEqual(settings.strategy.ema_fast, 20)
        self.assertEqual(settings.strategy.ema_slow, 50)

    def test_invalid_strategy_settings_fail_early(self) -> None:
        for fields in ({"ema_fast": 50}, {"rsi_period": 1}, {"ema_fast": True},
                       {"buy_rsi_min": float("nan")}, {"sell_rsi_level": 100}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                StrategySettings(**fields)

    def test_live_mode_and_duplicate_symbols_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for config in ('mode="live"\nsymbols=["FPT"]', 'symbols=["FPT", "FPT"]'):
                path.write_text(config, encoding="utf-8")
                with self.subTest(config=config), self.assertRaises(ValueError):
                    load_settings(path)


if __name__ == "__main__":
    unittest.main()
