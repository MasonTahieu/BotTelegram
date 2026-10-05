"""Daily-only preferences, quote isolation, and Telegram delivery."""
import io
import json
import math
import socket
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fintech_bot.app import build_application
from fintech_bot.bot.telegram import TelegramClient, TelegramError, TelegramRunner, render_chart, split_message, load_token
from fintech_bot.config import Settings, StrategySettings, load_settings
from fintech_bot.data.daily_cache import LocalDailyCache
from fintech_bot.data.vietcap_http import read_json
from fintech_bot.data.vietcap_quotes import quote_candle
from fintech_bot.domain import Candle, Instrument, Signal, SignalSide, VIETNAM
from fintech_bot.market import MarketCalendar
from fintech_bot.services.financial_filter import calculate_amihud
from fintech_bot.services.live_data import LiveData, refresh_quotes
from fintech_bot.storage.lock import ProcessLock
from fintech_bot.storage.sqlite import SqliteRepository
from tests.test_project import RecordingNotifier


def quote(symbol="FPT", board="HSX", observed="2026-09-18T10:00:00+07:00", source=None):
    source = source or observed
    return {"schema": 1, "fetched_at": observed, "data": {
        "listingInfo": {"symbol": symbol, "board": board, "stockType": "STOCK", "isDelisted": 0,
                        "tradingStatus": "TRADING_ACTIVATED", "tradingDate": "2026-09-18"},
        "matchPrice": {"symbol": symbol, "time": source, "openPrice": 10000,
                       "highest": 10500, "lowest": 9900, "matchPrice": 10100, "accumulatedVolume": 100},
        "bidAsk": {"symbol": symbol, "time": source}}}


class ChoicesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = build_application(Settings(("FPT", "HPG"), self.root / "app.db", StrategySettings()), RecordingNotifier())
        self.addCleanup(self.app.close)

    def test_legacy_five_minute_user_keeps_subscription(self):
        path = self.root / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE preferences(chat_id TEXT PRIMARY KEY,mode TEXT,timeframe TEXT);"
                           "INSERT INTO preferences VALUES ('old','all','5m');"
                           "CREATE TABLE subscriptions(chat_id TEXT,symbol TEXT,PRIMARY KEY(chat_id,symbol));"
                           "INSERT INTO subscriptions VALUES ('old','FPT');")
        conn.close()
        db = SqliteRepository(path)
        self.addCleanup(db.close)
        self.assertEqual(db.preference("old"), ("all", "1d"))
        self.assertEqual(db.subscriptions("old"), ["FPT"])
        self.assertEqual(load_settings().scan_interval_seconds, 900)

    def test_financial_filter_only_checks_when_enabled(self):
        self.app.commands.handle("/filter roe_min 15", "alice")
        self.assertTrue(self.app.alerts.financial_filter.check("alice", "FPT")[0])
        self.assertTrue(self.app.alerts.financial_filter.check("bob", "FPT")[0])
        self.app.commands.handle("/filter roe_min 99", "alice")
        self.assertFalse(self.app.alerts.financial_filter.check("alice", "FPT")[0])
        self.app.commands.handle("/filter clear", "alice")
        self.assertTrue(self.app.alerts.financial_filter.check("alice", "FPT")[0])

    def test_amihud_uses_20_closed_daily_returns_and_traded_value(self):
        start = date(2026, 8, 1)
        candles = [Candle("FPT", start + timedelta(days=i), 100, 110, 90,
                          100 * 1.01 ** i, 1000) for i in range(21)]
        expected = sum(abs(math.log(candles[i].close / candles[i - 1].close))
                       / (candles[i].close * candles[i].volume)
                       for i in range(1, 21)) / 20
        self.assertAlmostEqual(calculate_amihud(candles), expected)
        self.assertIsNone(calculate_amihud(candles[:20]))
        unclosed = Candle("FPT", start + timedelta(days=21), 100, 110, 90,
                          999, 1000, is_closed=False)
        self.assertAlmostEqual(calculate_amihud([*candles, unclosed]), expected)
        self.assertIsNone(calculate_amihud([*candles[:-1],
                          Candle("FPT", candles[-1].session, 100, 110, 90,
                                 candles[-1].close, 0)]))

    def test_illiq_filter_screen_buy_sell_and_financials(self):
        value = self.app.alerts.financial_filter.illiq("FPT")
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)
        self.assertIn("/filter illiq_max <gi\u00e1 tr\u1ecb>", self.app.commands.handle("/help", "alice"))
        self.assertIn("illiq_max=0", self.app.commands.handle("/filter illiq_max 0", "alice"))
        with patch.object(self.app.scanner.provider, "get_financials",
                          side_effect=AssertionError("liquidity-only filter fetched financials")):
            self.assertFalse(self.app.alerts.financial_filter.check("alice", "FPT")[0])
            self.assertIn("0/2 m\u00e3", self.app.commands.handle("/screen", "alice"))
        report = self.app.scanner.run()
        buy = next(signal for signal in report.signals if signal.side == SignalSide.BUY)
        sell = next(signal for signal in report.signals if signal.side == SignalSide.SELL)
        self.assertFalse(self.app.alerts.financially_eligible("alice", buy))
        self.assertTrue(self.app.alerts.financially_eligible("alice", sell))
        signals = self.app.commands.handle("/signals", "alice")
        self.assertIn("HPG", signals)
        self.assertNotIn("FPT", signals)
        self.assertIn("Amihud ILLIQ (20 phi\u00ean):", self.app.commands.handle("/financials FPT", "alice"))
        self.app.commands.handle("/filter illiq_max 1", "alice")
        self.assertIn("2/2 m\u00e3", self.app.commands.handle("/screen", "alice"))

    def test_early_command_is_gone_and_signals_are_confirmed(self):
        self.assertIn("chưa được hỗ trợ", self.app.commands.handle("/early off"))
        self.assertIn("đã ngừng hỗ trợ", self.app.commands.handle("/timeframe 5m"))
        self.assertTrue(all(signal.confirmed for signal in self.app.scanner.run().signals))


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(("FPT", "MBB", "DCM"), self.root / "app.db",
                                 StrategySettings(), vnstock_path=self.root / "vnstock")
        self.app = build_application(self.settings, RecordingNotifier())
        self.addCleanup(self.app.close)
        self.cache = LocalDailyCache(self.root / "daily")
        for symbol in self.settings.symbols:
            self.cache.write(symbol, self.app.scanner.provider.get_candles(symbol, "1d"),
                             "VNSTOCK_KBS", self.app.clock.now())

    def test_chart_renders_three_symbols_and_marks_latest_signal(self):
        for symbol in ("FPT", "MBB", "DCM"):
            path = self.root / (symbol + ".png")
            render_chart(self.app, symbol, path)
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        before = (self.root / "FPT.png").read_bytes()
        candle = self.cache.read("FPT")[0][-2]
        self.app.repository.save_signal(Signal(
            "FPT", candle.session, SignalSide.BUY, self.settings.strategy.strategy_id,
            "test", {}, "DEMO", True, closed_at=candle.closed_at))
        render_chart(self.app, "FPT", self.root / "FPT.png")
        self.assertNotEqual(before, (self.root / "FPT.png").read_bytes())
        self.assertIn("/chart <M\u00c3>", self.app.commands.handle("/help"))

    def test_telegram_chart_sends_photo_deletes_temp_and_reports_missing(self):
        class Client:
            bot_id = "123456"
            messages = []
            photos = []
            paths = []
            texts = []
            def updates(self, offset):
                return [item for item in self.messages if item["update_id"] >= offset]
            def send_photo(self, chat, path):
                self.paths.append(Path(path))
                self.photos.append((chat, Path(path).read_bytes()))
            def send_part(self, chat, text):
                self.texts.append((chat, text))
        client = Client()
        client.messages = [
            {"update_id": i, "message": {"chat": {"id": i, "type": "private"},
                                         "text": f"/chart {symbol}"}}
            for i, symbol in enumerate(("FPT", "MBB", "DCM"), 1)]
        runner = TelegramRunner(self.app, client, "example_bot")
        runner.once()
        self.assertEqual(len(client.photos), 3)
        self.assertTrue(all(data.startswith(b"\x89PNG") for _, data in client.photos))
        self.assertTrue(all(not path.exists() for path in client.paths))
        self.assertEqual(runner.state["offset"], 4)

        (self.root / "daily" / "DCM-daily.json").unlink()
        client.messages.extend([
            {"update_id": 4, "message": {"chat": {"id": 4, "type": "private"},
                                         "text": "/chart DCM"}},
            {"update_id": 5, "message": {"chat": {"id": 5, "type": "private"},
                                         "text": "/chart ZZZ"}},
        ])
        runner.once()
        self.assertEqual(len(client.photos), 3)
        self.assertTrue(any("ch\u01b0a READY" in text for _, text in client.texts))
        self.assertTrue(any("universe" in text for _, text in client.texts))
        self.assertEqual(runner.state["offset"], 6)

    def test_chart_retry_removes_each_temporary_file(self):
        class Client:
            bot_id = "123456"
            attempts = []
            fail = True
            def updates(self, offset):
                return ([{"update_id": 1, "message": {"chat": {"id": 10, "type": "private"},
                                                       "text": "/chart FPT"}}]
                        if offset <= 1 else [])
            def send_photo(self, chat, path):
                self.attempts.append(Path(path))
                if self.fail:
                    raise TelegramError("temporary send failure")
            def send_part(self, chat, text):
                raise AssertionError("Chart should be a photo")
        client = Client()
        runner = TelegramRunner(self.app, client, "example_bot")
        with self.assertRaises(TelegramError):
            runner.once()
        self.assertFalse(client.attempts[0].exists())
        self.assertEqual(runner.state["pending"]["chart"], "FPT")
        client.fail = False
        runner.once()
        self.assertEqual(len(client.attempts), 2)
        self.assertTrue(all(not path.exists() for path in client.attempts))
        self.assertEqual(runner.state["offset"], 2)

    def test_send_photo_uses_multipart(self):
        path = self.root / "chart.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        TelegramClient(TOKEN, opener=opener).send_photo("123", path)
        self.assertIn("/sendPhoto", calls[0].full_url)
        self.assertIn("multipart/form-data", calls[0].get_header("Content-type"))
        self.assertIn(path.read_bytes(), calls[0].data)



class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calendar = MarketCalendar()

    def test_quote_preserves_trade_time_and_does_not_touch_history(self):
        sample = quote(observed="2026-09-18T10:30:00+07:00", source="2026-09-18T10:00:00+07:00")
        candle = quote_candle(sample, "FPT", "HOSE", self.calendar)
        self.assertEqual(candle.observed_at, datetime(2026, 9, 18, 10, tzinfo=VIETNAM))
        self.assertFalse(candle.is_closed)
        self.assertFalse(list(self.root.glob("*daily*")))

    def test_partial_batch_keeps_success_and_marks_missing(self):
        sample = quote()
        class Client:
            def quotes(self, symbols):
                return {**sample, "data": [sample["data"]]}
        report = refresh_quotes(self.root, [Instrument("FPT", "HOSE"), Instrument("SHS", "HNX")], self.calendar, Client())
        self.assertEqual(report["valid"], 1)
        self.assertTrue((self.root / "FPT-quote.json").exists())
        self.assertIn("SHS-quote", read_json(self.root / "failures.json"))
        self.assertFalse(list(self.root.glob("*daily*")))

    def test_process_lock_prevents_overlapping_collectors(self):
        with ProcessLock(self.root / "job.lock"):
            with self.assertRaises(ValueError):
                with ProcessLock(self.root / "job.lock"):
                    self.fail("overlap")
        with ProcessLock(self.root / "job.lock"):
            pass

    def test_live_tick_does_not_start_another_worker_while_running(self):
        settings = Settings(("FPT",), self.root / "db", StrategySettings(), mode="vietcap", vietcap_path=self.root)
        live = LiveData(settings, [Instrument("FPT", "HOSE")], self.calendar)
        class Running:
            def is_alive(self): return True
        live.thread = Running()
        with patch("fintech_bot.services.live_data.threading.Thread") as factory:
            self.assertIsNone(live.tick())
            factory.assert_not_called()


TOKEN = "123456:" + "a" * 30


class TelegramTests(unittest.TestCase):
    def test_telegram_errors_do_not_include_token(self):
        def broken(request, **kwargs):
            raise urllib.error.URLError(request.full_url)
        with self.assertRaises(TelegramError) as failure:
            TelegramClient(TOKEN, opener=broken).call("getMe")
        self.assertNotIn(TOKEN, str(failure.exception))

    def test_long_poll_has_bounded_http_timeout(self):
        calls = []
        def response(request, **kwargs):
            calls.append((json.loads(request.data), kwargs["timeout"]))
            return io.BytesIO(json.dumps({"ok": True, "result": []}).encode())
        self.assertEqual(TelegramClient(TOKEN, opener=response).updates(0), [])
        self.assertEqual(calls[0][0]["timeout"], 25)
        self.assertEqual(calls[0][1], 37)

    def test_timeout_dns_and_429_are_distinct_and_safe(self):
        for failure, expected in ((TimeoutError("slow"), "TimeoutError"),
                                  (urllib.error.URLError(socket.gaierror("dns")), "gaierror")):
            def broken(request, **kwargs):
                raise failure
            with self.assertRaises(TelegramError) as raised:
                TelegramClient(TOKEN, opener=broken).updates(0)
            self.assertEqual(raised.exception.exception_type, expected)
            self.assertFalse(raised.exception.permanent)
            self.assertNotIn(TOKEN, str(raised.exception))

        def limited(request, **kwargs):
            return io.BytesIO(json.dumps({"ok": False, "error_code": 429,
                                          "parameters": {"retry_after": 2}}).encode())
        with self.assertRaises(TelegramError) as raised:
            TelegramClient(TOKEN, opener=limited).updates(0)
        self.assertEqual(raised.exception.retry_after, 2)

    def test_unicode_split_and_missing_token(self):
        value = "Thông báo 😀 " * 1000
        parts = split_message(value)
        self.assertEqual("".join(parts), value)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts))
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Chưa có token"):
                load_token(Path(folder) / "missing")

    def test_command_delivery_progress_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            app = build_application(Settings(("FPT",), Path(folder) / "app.db", StrategySettings()), RecordingNotifier())
            self.addCleanup(app.close)
            class FakeClient:
                bot_id = "123456"
                sent = []
                fail_second = True
                def updates(self, offset):
                    return [{"update_id": 7, "message": {"chat": {"id": 88, "type": "private"}, "text": "/help@example_bot"}}] if offset <= 7 else []
                def send_part(self, chat, message):
                    if len(self.sent) == 1 and self.fail_second:
                        raise TelegramError("Network")
                    self.sent.append(message)
            client = FakeClient()
            runner = TelegramRunner(app, client, "example_bot")
            with patch.object(app.commands, "handle", return_value="a" * 4000) as handler:
                with self.assertRaises(TelegramError):
                    runner.once()
                client.fail_second = False
                restarted = TelegramRunner(app, client, "example_bot")
                restarted.once()
                self.assertEqual(handler.call_count, 1)
            self.assertEqual("".join(client.sent), "a" * 4000)
            self.assertEqual(restarted.state["offset"], 8)
            app.close()


if __name__ == "__main__":
    unittest.main()
