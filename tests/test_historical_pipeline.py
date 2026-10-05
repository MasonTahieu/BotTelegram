import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fintech_bot.config import Settings, StrategySettings
from fintech_bot.data.daily_cache import LocalDailyCache
from fintech_bot.data.vietcap import parse_candles
from fintech_bot.data.vnstock_sync import bootstrap
from fintech_bot.domain import Instrument, VIETNAM
from fintech_bot.market import MarketCalendar, ReplayClock
from fintech_bot.data.replay import ReplayDataProvider
from tests.test_vietcap import prices, universe


class HistoricalBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vietcap = self.root / "vietcap"
        self.vnstock = self.root / "vnstock"
        self.vietcap.mkdir()
        self.vnstock.mkdir()
        self.now = datetime(2026, 9, 18, 16, tzinfo=VIETNAM)
        self.calendar = MarketCalendar()
        self.instruments = [Instrument("FPT", "HOSE"), Instrument("SHS", "HNX"),
                            Instrument("ACV", "UPCOM")]
        self.settings = Settings(
            (), self.root / "app.db", StrategySettings(), mode="vietcap",
            vietcap_path=self.vietcap, vnstock_path=self.vnstock,
            primary_provider="vnstock", enable_failover=True,
        )

    def test_kbs_then_vietcap_checkpoint_resume_and_failure_groups(self):
        replay = ReplayDataProvider(self.instruments, ReplayClock(self.now), self.calendar)

        class Backup:
            instruments = self.instruments

        class Primary:
            source = "VNSTOCK_KBS"
            api_call_count = 0
            calls = []

            def get_candles(provider_self, symbol, timeframe):
                provider_self.calls.append(symbol)
                provider_self.api_call_count += 1
                if symbol != "FPT":
                    raise TimeoutError("primary timeout")
                return replay.get_candles(symbol, timeframe)

        by_symbol = {item.symbol: item for item in self.instruments}

        class Client:
            requests = 0
            calls = []

            def candles(client_self, symbol, timeframe, count, *, to_timestamp=None):
                client_self.calls.append(symbol)
                client_self.requests += 1
                if symbol == "ACV":
                    raise ValueError("provider error")
                item = by_symbol[symbol]
                stamps = [value.replace(hour=7, minute=0)
                          for value in self.calendar.history_ends(self.now, item.exchange, "1d", 60)]
                return prices(symbol, stamps=stamps, now=self.now)

        primary, client = Primary(), Client()
        report = bootstrap(
            self.settings, retries=0, timeout=3, backoff=0, target_bars=250,
            primary=primary, backup=Backup(), client=client, now=self.now,
            progress=lambda _: None,
        )
        self.assertEqual(report["ready"], 2)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["sources"], {"VNSTOCK_KBS": 1, "VIETCAP_DIRECT": 1})
        self.assertEqual(primary.calls, ["FPT", "SHS", "ACV"])
        self.assertTrue(any("ACV" in values for values in report["failure_groups"].values()))
        cache = LocalDailyCache(self.root / "daily")
        self.assertGreaterEqual(len(cache.read("FPT")[0]), 52)
        self.assertGreaterEqual(len(cache.read("SHS")[0]), 52)

        calls = (len(primary.calls), len(client.calls))
        resumed = bootstrap(
            self.settings, retries=0, timeout=3, backoff=0, target_bars=250,
            primary=primary, backup=Backup(), client=client, now=self.now,
            progress=lambda _: None,
        )
        self.assertEqual(resumed["reused"], 2)
        # Resume continues after a same-day failure without spending quota on
        # the failed symbol again.  Retrying failures is explicit.
        self.assertEqual((len(primary.calls), len(client.calls)), calls)
        retried = bootstrap(
            self.settings, retries=0, timeout=3, backoff=0, target_bars=250,
            primary=primary, backup=Backup(), client=client, now=self.now,
            retry_failed=True, progress=lambda _: None,
        )
        self.assertEqual(retried["failed"], 1)
        self.assertEqual((len(primary.calls), len(client.calls)), (calls[0] + 1, calls[1] + 1))

    def test_after_close_yesterday_cache_is_not_reused(self):
        instrument = self.instruments[0]
        morning = self.now.replace(hour=10)
        old = ReplayDataProvider([instrument], ReplayClock(morning), self.calendar).get_candles("FPT", "1d")
        LocalDailyCache(self.root / "daily").write("FPT", old, "VNSTOCK_KBS", morning)
        current = ReplayDataProvider([instrument], ReplayClock(self.now), self.calendar).get_candles("FPT", "1d")

        class Backup:
            instruments = [instrument]

        class Primary:
            source = "VNSTOCK_KBS"
            calls = []
            def get_history_through(self, symbol, timeframe, end, *, start=None):
                self.calls.append((start, end))
                return [candle for candle in current if candle.session >= start]
            def get_candles(self, symbol, timeframe):
                raise AssertionError("Không được full bootstrap khi chỉ thiếu một phiên")

        class Client:
            requests = 0

        primary = Primary()
        report = bootstrap(self.settings, symbols=("FPT",), retries=0, timeout=3,
                           primary=primary, backup=Backup(), client=Client(), now=self.now,
                           progress=lambda _: None)
        self.assertEqual((report["updated"], report["reused"], report["bootstrapped"]), (1, 0, 0))
        self.assertEqual(primary.calls, [(old[-5].session, self.now.date())])

    def test_five_symbols_reuse_incremental_and_bootstrap_before_market_close(self):
        now = datetime(2026, 9, 22, 10, tzinfo=VIETNAM)
        instruments = [Instrument(symbol, "HOSE") for symbol in
                       ("FPT", "MBB", "HPG", "VCB", "SSI")]
        replay = ReplayDataProvider(instruments, ReplayClock(now), self.calendar)
        full = {item.symbol: replay.get_candles(item.symbol, "1d") for item in instruments}
        expected = self.calendar.latest_end(now, "HOSE", "1d")
        cache = LocalDailyCache(self.root / "daily")
        for symbol in ("FPT", "MBB"):
            cache.write(symbol, full[symbol], "VNSTOCK_KBS", now)
        cache.write("HPG", full["HPG"][:-1], "VNSTOCK_KBS", now)
        stamps = [end.replace(hour=7, minute=0) for end in
                  self.calendar.history_ends(now, "HOSE", "1d", 60)]
        vietcap_full = parse_candles(prices("VCB", stamps=stamps, now=now),
                                     "VCB", "HOSE", "1d", self.calendar)
        cache.write("VCB", vietcap_full[:-2], "VIETCAP_DIRECT", now)

        class Backup:
            pass

        class Primary:
            source = "VNSTOCK_KBS"
            calls = []
            def get_history_through(self, symbol, timeframe, end, *, start=None):
                self.calls.append((symbol, start, end))
                return ([candle for candle in full[symbol] if candle.session >= start]
                        if start is not None else full[symbol])
            def get_candles(self, symbol, timeframe):
                raise AssertionError("Unbounded KBS request")

        class Client:
            requests = 0
            calls = []
            def candles(self, symbol, timeframe, count, *, to_timestamp=None):
                self.requests += 1
                self.calls.append((symbol, count, to_timestamp))
                return prices(symbol, stamps=stamps[-count:], now=now)

        primary, client, backup = Primary(), Client(), Backup()
        backup.instruments = instruments
        report = bootstrap(self.settings, symbols=tuple(item.symbol for item in instruments),
                           retries=0, timeout=3, primary=primary, backup=backup,
                           client=client, now=now, progress=lambda _: None)
        self.assertEqual((report["universe"], report["updated"], report["reused"],
                          report["bootstrapped"], report["failed"]), (5, 2, 2, 1, 0))
        self.assertEqual(report["ready"] + report["reused"], 5)
        self.assertEqual(report["results"], {
            "FPT": "REUSED", "MBB": "REUSED", "HPG": "UPDATED",
            "VCB": "UPDATED", "SSI": "BOOTSTRAPPED",
        })
        self.assertEqual({(symbol, end) for symbol, _, end in primary.calls},
                         {("HPG", expected.date()), ("SSI", expected.date())})
        self.assertEqual(client.calls, [("VCB", 7, expected.timestamp())])
        self.assertEqual(len(cache.read("HPG")[0]), len(full["HPG"]))
        self.assertEqual(len(cache.read("VCB")[0]), len(vietcap_full))
        for symbol in ("HPG", "VCB", "SSI"):
            self.assertEqual(cache.read(symbol)[0][-1].timestamp, expected)
        calls = (len(primary.calls), len(client.calls))
        again = bootstrap(self.settings, symbols=tuple(item.symbol for item in instruments),
                          retries=0, timeout=3, primary=primary, backup=backup,
                          client=client, now=now, resume=False, progress=lambda _: None)
        self.assertEqual((again["reused"], again["updated"], again["bootstrapped"]),
                         (5, 0, 0))
        self.assertEqual((len(primary.calls), len(client.calls)), calls)

    def test_batch_quote_skips_only_one_missing_no_trade_session(self):
        now = datetime(2026, 9, 22, 16, tzinfo=VIETNAM)
        instruments = [Instrument(symbol, "HOSE") for symbol in
                       ("FPT", "MBB", "HPG", "VCB", "SSI")]
        replay = ReplayDataProvider(instruments, ReplayClock(now), self.calendar)
        full = {item.symbol: replay.get_candles(item.symbol, "1d") for item in instruments}
        expected = self.calendar.latest_end(now, "HOSE", "1d")
        cache = LocalDailyCache(self.root / "daily")
        for symbol, candles in (("FPT", full["FPT"]), ("MBB", full["MBB"][:-1]),
                                ("HPG", full["HPG"][:-1]), ("VCB", full["VCB"][:-2])):
            cache.write(symbol, candles, "VNSTOCK_KBS", now)

        class Backup:
            pass

        class Primary:
            source = "VNSTOCK_KBS"
            calls = []
            def get_history_through(self, symbol, timeframe, end, *, start=None):
                self.calls.append((symbol, start, end))
                return ([candle for candle in full[symbol] if candle.session >= start]
                        if start is not None else full[symbol])
            def get_candles(self, symbol, timeframe):
                raise AssertionError("Unbounded KBS request")

        class Client:
            requests = 0
            quote_calls = []
            stale = False
            def quotes(self, symbols):
                self.requests += 1
                self.quote_calls.append(tuple(symbols))
                fetched = expected + timedelta(seconds=30) if self.stale else now
                rows = []
                for symbol in symbols:
                    traded = symbol == "HPG"
                    rows.append({
                        "listingInfo": {"symbol": symbol, "board": "HSX",
                                        "stockType": "STOCK", "isDelisted": 0,
                                        "tradingStatus": "TRADING_ACTIVATED",
                                        "tradingDate": expected.date().isoformat()},
                        "matchPrice": {"symbol": symbol,
                                       "time": now.replace(hour=14).isoformat(),
                                       "openPrice": 100 if traded else None,
                                       "highest": 101 if traded else 0,
                                       "lowest": 99 if traded else 0,
                                       "matchPrice": 100 if traded else 0,
                                       "accumulatedVolume": 100 if traded else 0},
                    })
                return {"schema": 1, "fetched_at": fetched.isoformat(), "data": rows}
            def candles(self, symbol, timeframe, count, *, to_timestamp=None):
                raise AssertionError("Unexpected Vietcap historical request")

        backup, primary, client = Backup(), Primary(), Client()
        backup.instruments = instruments
        report = bootstrap(self.settings, symbols=tuple(item.symbol for item in instruments),
                           retries=0, primary=primary, backup=backup, client=client,
                           now=now, progress=lambda _: None)
        self.assertEqual(report["results"], {
            "FPT": "REUSED", "MBB": "NO_NEW_CANDLE", "HPG": "UPDATED",
            "VCB": "UPDATED", "SSI": "BOOTSTRAPPED"})
        self.assertEqual((report["reused"], report["no_new_candle"], report["updated"],
                          report["bootstrapped"], report["failed"], report["ready"]),
                         (1, 1, 2, 1, 0, 4))
        self.assertEqual(client.quote_calls, [("MBB", "HPG")])
        self.assertEqual({symbol for symbol, _, _ in primary.calls}, {"HPG", "VCB", "SSI"})
        self.assertEqual(len(cache.read("MBB")[0]), len(full["MBB"]) - 1)
        self.assertGreaterEqual(cache.read("MBB")[1], expected)
        self.assertIn("MBB/1d", json.loads(
            (self.vnstock / "bootstrap-progress.json").read_text(encoding="utf-8"))["completed"])

        client.stale = True
        second = bootstrap(self.settings, symbols=("MBB",), retries=0, primary=primary,
                           backup=backup, client=client, now=now, resume=False,
                           progress=lambda _: None)
        self.assertEqual(second["results"]["MBB"], "UPDATED")
        self.assertEqual(primary.calls[-1][0], "MBB")

    def test_incremental_failure_keeps_valid_cache_without_full_bootstrap(self):
        now = datetime(2026, 9, 22, 10, tzinfo=VIETNAM)
        instrument = Instrument("FPT", "HOSE")
        full = ReplayDataProvider([instrument], ReplayClock(now), self.calendar).get_candles("FPT", "1d")
        cache = LocalDailyCache(self.root / "daily")
        cache.write("FPT", full[:-1], "VNSTOCK_KBS", now)

        class Backup:
            instruments = [instrument]
        class Primary:
            source = "VNSTOCK_KBS"
            def get_history_through(self, symbol, timeframe, end, *, start=None):
                raise TimeoutError("KBS timeout")
            def get_candles(self, symbol, timeframe):
                raise AssertionError("Valid cache must not trigger full bootstrap")
        class Client:
            requests = 0
            def candles(self, symbol, timeframe, count, *, to_timestamp=None):
                raise AssertionError("Valid cache must not trigger full fallback")

        report = bootstrap(self.settings, symbols=("FPT",), retries=0, timeout=3,
                           primary=Primary(), backup=Backup(), client=Client(),
                           now=now, progress=lambda _: None)
        self.assertEqual((report["updated"], report["bootstrapped"], report["failed"]),
                         (0, 0, 1))
        self.assertEqual(cache.read("FPT")[0], full[:-1])

    def test_prepare_history_fetches_missing_universe_first(self):
        instrument = self.instruments[0]
        candles = ReplayDataProvider([instrument], ReplayClock(self.now), self.calendar).get_candles("FPT", "1d")

        class Primary:
            source = "VNSTOCK_KBS"
            def get_candles(self, symbol, timeframe):
                return candles

        class Client:
            requests = 0
            def universe(self):
                self.requests += 1
                return universe()

        client = Client()
        report = bootstrap(self.settings, symbols=("FPT",), retries=0, timeout=3,
                           primary=Primary(), client=client, now=self.now,
                           progress=lambda _: None)
        self.assertEqual(report["ready"], 1)
        self.assertEqual(client.requests, 1)
        self.assertTrue((self.vietcap / "universe.json").exists())

    def test_kbs_stale_series_falls_back_to_full_vietcap_series(self):
        instrument = self.instruments[0]
        stale = ReplayDataProvider([instrument], ReplayClock(self.now.replace(hour=10)), self.calendar).get_candles("FPT", "1d")
        stamps = [end.replace(hour=7, minute=0)
                  for end in self.calendar.history_ends(self.now, "HOSE", "1d", 60)]

        class Backup:
            instruments = [instrument]

        class Primary:
            source = "VNSTOCK_KBS"
            def get_candles(self, symbol, timeframe):
                return stale

        class Client:
            requests = 0
            def candles(self, symbol, timeframe, count, *, to_timestamp=None):
                self.requests += 1
                return prices(symbol, stamps=stamps, now=self_now)

        self_now = self.now
        client = Client()
        report = bootstrap(self.settings, symbols=("FPT",), retries=0, timeout=3,
                           primary=Primary(), backup=Backup(), client=client, now=self.now,
                           progress=lambda _: None)
        self.assertEqual((report["ready"], client.requests), (1, 1))
        self.assertEqual(LocalDailyCache(self.root / "daily").read("FPT")[2], "VIETCAP_DIRECT")

    def test_vietcap_fallback_accepts_fresh_source_without_trade_in_latest_session(self):
        instrument = self.instruments[0]
        stale = ReplayDataProvider([instrument], ReplayClock(self.now.replace(hour=10)), self.calendar).get_candles("FPT", "1d")
        # Provider was refreshed after the latest market close, but this symbol
        # did not trade in the latest session, so its final candle is one session older.
        stamps = [end.replace(hour=7, minute=0)
                  for end in self.calendar.history_ends(self.now, "HOSE", "1d", 61)[:-1]]

        class Backup:
            instruments = [instrument]

        class Primary:
            source = "VNSTOCK_KBS"
            def get_candles(self, symbol, timeframe):
                return stale

        class Client:
            requests = 0
            def candles(self, symbol, timeframe, count, *, to_timestamp=None):
                self.requests += 1
                return prices(symbol, stamps=stamps, now=self_now)

        self_now = self.now
        client = Client()
        report = bootstrap(self.settings, symbols=("FPT",), retries=0, timeout=3,
                           primary=Primary(), backup=Backup(), client=client, now=self.now,
                           progress=lambda _: None)
        self.assertEqual((report["ready"], report["failed"], client.requests), (1, 0, 1))
        candles, _, source = LocalDailyCache(self.root / "daily").read("FPT")
        self.assertEqual(source, "VIETCAP_DIRECT")
        self.assertLess(candles[-1].timestamp, self.calendar.latest_end(self.now, "HOSE", "1d"))


if __name__ == "__main__":
    unittest.main()
