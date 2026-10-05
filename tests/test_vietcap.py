"""Provider and integration regressions. All network responses are controlled fakes."""

import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fintech_bot.app import build_application
from fintech_bot.config import Settings, StrategySettings, load_settings
from fintech_bot.data.vietcap import (VietcapCacheProvider, parse_candles, parse_financials, parse_universe)
from fintech_bot.data.vietcap_http import VietcapClient, SourceUnavailable, atomic_json, read_json
from fintech_bot.data.vietcap_sync import collect
from fintech_bot.domain import Signal, SignalSide, VIETNAM
from fintech_bot.market import MarketCalendar, ReplayClock
from fintech_bot.storage.sqlite import SqliteRepository
from fintech_bot.services.backtest import run_backtest
from fintech_bot.strategies.ema_rsi import EmaRsiStrategy

NOW = datetime(2026, 9, 20, 12, tzinfo=VIETNAM)
DAY = date(2026, 9, 18)


def envelope(data, now=NOW):
    return {"schema": 1, "fetched_at": now.isoformat(), "data": data}


def universe():
    return envelope([{"symbol": symbol, "board": exchange, "type": kind, "organName": "Test"}
                     for symbol, exchange, kind in (("FPT", "HSX", "STOCK"), ("SHS", "HNX", "STOCK"),
                                                   ("ACV", "UPCOM", "STOCK"), ("ETF", "HSX", "FUND"))])


def prices(symbol="FPT", stamps=None, now=NOW):
    stamps = stamps or [datetime(2026, 9, 18, 7, tzinfo=VIETNAM)]
    return envelope([{"symbol": symbol, "t": [str(int(t.timestamp())) for t in stamps],
                      "o": [10000] * len(stamps), "h": [10500] * len(stamps),
                      "l": [9900] * len(stamps), "c": [10100] * len(stamps), "v": [100] * len(stamps)}], now)


def financials(symbol="FPT", organ="FPT"):
    ratios = [{"organCode": organ, "yearReport": 2026, "quarter": 2, "ratioType": "RATIO_TTM",
               "pe": 12, "pb": 2, "roe": .2, "debtToEquity": .5},
              {"organCode": organ, "yearReport": 2025, "quarter": 5, "ratioType": "RATIO_YEAR",
               "pe": 13, "pb": 3, "roe": .15, "debtToEquity": .6}]
    income = {"years": [{"ticker": symbol, "organCode": organ, "yearReport": 2025, "lengthReport": 5,
                          "publicDate": "2026-03-20T00:00:00", "isa23": 4000}],
              "quarters": [{"ticker": symbol, "organCode": organ, "yearReport": 2026, "lengthReport": 2,
                            "publicDate": "2026-08-22T00:00:00", "isa23": 1500}]}
    return envelope({"data": ratios}), envelope({"data": income})


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.calendar = MarketCalendar()

    def test_universe_filters_non_stocks_and_maps_hsx(self):
        items = parse_universe(universe())
        self.assertEqual({x.symbol: x.exchange for x in items}, {"FPT": "HOSE", "SHS": "HNX", "ACV": "UPCOM"})

    def test_daily_dates_are_session_labels_and_prices_stay_in_vnd(self):
        for symbol, exchange, hour, minute in (("FPT", "HOSE", 14, 45), ("SHS", "HNX", 15, 0)):
            candle = parse_candles(prices(symbol), symbol, exchange, "1d", self.calendar)[0]
            self.assertEqual(candle.timestamp, datetime(2026, 9, 18, hour, minute, tzinfo=VIETNAM))
            self.assertEqual(candle.close, 10100)
            self.assertTrue(candle.is_closed)

    def test_daily_preview_freezes_observation_time(self):
        now = datetime(2026, 9, 18, 10, tzinfo=VIETNAM)
        candle = parse_candles(prices(now=now), "FPT", "HOSE", "1d", self.calendar)[0]
        self.assertFalse(candle.is_closed)
        self.assertEqual(candle.observed_at, now)

    def test_minute_data_is_not_supported(self):
        start = datetime(2026, 9, 18, 9, 15, tzinfo=VIETNAM)
        stamps = [start + timedelta(minutes=i) for i in range(10)]
        with self.assertRaises(ValueError):
            parse_candles(prices(stamps=stamps), "FPT", "HOSE", "5m", self.calendar)

    def test_wrong_symbol_unequal_columns_and_bad_values_rejected(self):
        for change in (lambda d: d[0].update(symbol="HPG"), lambda d: d[0]["c"].append(100),
                       lambda d: d[0]["v"].__setitem__(0, 1.5), lambda d: d[0]["c"].__setitem__(0, float("nan")),
                       lambda d: d[0]["c"].__setitem__(0, 999999)):
            data = prices()
            change(data["data"])
            with self.assertRaises(ValueError):
                parse_candles(data, "FPT", "HOSE", "1d", self.calendar)
        with self.assertRaises(ValueError):
            parse_candles(envelope([]), "FPT", "HOSE", "1d", self.calendar)

    def test_financial_organ_identifier_need_not_equal_ticker(self):
        ratios, income = financials("ACV", "ACVN")
        snapshots = parse_financials(ratios, income, "ACV")
        self.assertEqual(len(snapshots), 2)
        latest = snapshots[-1]
        self.assertEqual((latest.period, latest.eps, latest.roe), ("2026-Q2", 1500, .2))
        self.assertEqual(latest.published_on, date(2026, 8, 22))
        self.assertEqual(latest.observed_on, NOW.date())
        self.assertEqual(latest.ratio_basis, "RATIO_TTM")

    def test_missing_publication_dates_not_invented(self):
        ratios, income = financials()
        for rows in income["data"]["data"].values():
            for row in rows:
                row["publicDate"] = None
        with self.assertRaisesRegex(ValueError, "ngày công bố"):
            parse_financials(ratios, income, "FPT")

    def test_financial_wrong_symbol_and_nan_rejected(self):
        ratios, income = financials()
        ratios["data"]["data"][0]["organCode"] = "HPG"
        with self.assertRaises(ValueError):
            parse_financials(ratios, income, "FPT")
        ratios, income = financials()
        ratios["data"]["data"][0]["roe"] = float("inf")
        with self.assertRaises(ValueError):
            parse_financials(ratios, income, "FPT")


class HttpTests(unittest.TestCase):
    def test_single_symbol_payload_no_credentials_and_bounded_response(self):
        seen = []
        def opener(request, timeout):
            seen.append((request, timeout))
            return io.BytesIO(b"[]")
        client = VietcapClient(opener=opener, sleep=lambda _: None)
        result = client.candles("FPT", "1d", 250, to_timestamp=1234567890)
        request, timeout = seen[0]
        self.assertEqual(json.loads(request.data)["symbols"], ["FPT"])
        self.assertEqual(json.loads(request.data)["to"], 1234567890)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(result["requested_bars"], 250)
        self.assertEqual(timeout, 15)

    def test_rate_limit_stops_following_calls_without_retry(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "60"}, None)
        client = VietcapClient(opener=opener, sleep=lambda _: None)
        with self.assertRaisesRegex(SourceUnavailable, "429"):
            client.universe()
        with self.assertRaises(SourceUnavailable):
            client.candles("FPT", "1d", 250)
        self.assertEqual(len(calls), 1)

    def test_transient_failure_retries_only_once(self):
        count = 0
        def opener(request, timeout):
            nonlocal count
            count += 1
            raise TimeoutError("timeout")
        client = VietcapClient(opener=opener, sleep=lambda _: None)
        with self.assertRaises(ValueError):
            client.universe()
        self.assertEqual(count, 2)


class CacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        atomic_json(self.path / "universe.json", universe())
        self.calendar = MarketCalendar()
        stamps = [x.replace(hour=7, minute=0) for x in self.calendar.history_ends(NOW, "HOSE", "1d", 60)]
        atomic_json(self.path / "FPT-daily.json", prices(stamps=stamps))
        ratios, income = financials()
        atomic_json(self.path / "FPT-ratios.json", ratios)
        atomic_json(self.path / "FPT-income.json", income)

    def test_atomic_replacement_updates_provider_and_failure_invalidates_cache(self):
        provider = VietcapCacheProvider(self.path, self.calendar)
        self.assertEqual(provider.get_candles("FPT")[-1].close, 10100)
        data = read_json(self.path / "FPT-daily.json")
        data["data"][0]["c"][-1] = 10200
        atomic_json(self.path / "FPT-daily.json", data)
        self.assertEqual(provider.get_candles("FPT")[-1].close, 10200)
        atomic_json(self.path / "failures.json", {"FPT-daily": {"error": "timeout"}})
        with self.assertRaisesRegex(ValueError, "timeout"):
            provider.get_candles("FPT")
        atomic_json(self.path / "failures.json", {})
        self.assertEqual(provider.get_candles("FPT")[-1].close, 10200)

    def test_batch_isolates_missing_symbols_without_network(self):
        result = VietcapCacheProvider(self.path, self.calendar).get_candles_batch(("FPT", "SHS"), ("1d",))
        self.assertIn(("FPT", "1d"), result.candles)
        self.assertIn(("SHS", "1d"), result.errors)

    def test_application_reads_vietcap_updates_and_marks_next_day_stale(self):
        settings = Settings(("FPT",), self.path / "app.sqlite3", StrategySettings(),
                            mode="vietcap", vietcap_path=self.path, alert_mode="off")
        clock = ReplayClock(NOW)
        app = build_application(settings, clock=clock)
        self.addCleanup(app.close)
        report = app.scanner.run()
        self.assertNotIn("FPT/1d", report.errors)
        self.assertIn("Vietcap", app.commands.handle("/status"))
        self.assertIn("1,500.00", app.commands.handle("/financials FPT"))
        stored = app.repository.connection.execute("SELECT COUNT(*) FROM financials").fetchone()[0]
        self.assertEqual(stored, 1)
        clock.current = datetime(2026, 9, 21, 15, tzinfo=VIETNAM)
        self.assertIn("FPT/1d", app.scanner.run().errors)

    def test_resume_skips_good_data_but_retries_failures_and_bigger_history(self):
        for filename in ("universe.json", "FPT-daily.json"):
            data = read_json(self.path / filename)
            data["fetched_at"] = datetime.now(VIETNAM).isoformat()
            atomic_json(self.path / filename, data)
        outer = self
        class Client:
            interval, requests = 1, 0
            def universe(self):
                raise AssertionError("universe should be cached")
            def candles(self, symbol, timeframe, count):
                self.requests += 1
                data = read_json(outer.path / "FPT-daily.json")
                data["requested_bars"] = count
                return data
        client = Client()
        # Exact cached size 60; larger request 100 must perform a download.
        report = collect(self.path, components=("daily",), symbols=("FPT",), resume=True,
                         max_age=10**9, daily_bars=100, client=client, progress=lambda _: None)
        self.assertEqual(report["downloaded"], 1)
        report = collect(self.path, components=("daily",), symbols=("FPT",), resume=True,
                         max_age=10**9, daily_bars=100, client=client, progress=lambda _: None)
        self.assertEqual(report["reused"], 1)
        atomic_json(self.path / "failures.json", {"FPT-daily": {"error": "old failure"}})
        report = collect(self.path, components=("daily",), symbols=("FPT",), resume=True,
                         max_age=10**9, daily_bars=100, client=client, progress=lambda _: None)
        self.assertEqual(report["downloaded"], 1)
        self.assertEqual(read_json(self.path / "failures.json"), {})

    def test_default_settings_still_demo_and_vietcap_has_separate_database(self):
        self.assertEqual(load_settings().mode, "demo")
        with self.assertRaises(ValueError):
            Settings((), self.path / "a.sqlite", StrategySettings(), mode="vietcap")

    def test_backtest_date_window_retains_warmup_and_has_no_early_trades(self):
        provider = VietcapCacheProvider(self.path, self.calendar)
        candles = provider.get_candles("FPT")
        strategy = EmaRsiStrategy(StrategySettings())
        start = candles[-6].session
        result = run_backtest("FPT", candles, strategy, source=provider.source, is_demo=False, start_date=start)
        self.assertEqual(result["first_evaluation"][:10], start.isoformat())
        self.assertEqual(result["bars"], 60)
        self.assertTrue(all(trade["executed_at"][:10] >= start.isoformat() for trade in result["trades"]))
        with self.assertRaises(ValueError):
            run_backtest("FPT", candles, strategy, source=provider.source, is_demo=False, start_date=candles[1].session)

    def test_collection_interleaves_exchanges_without_duplicate_tasks(self):
        calls = []
        class Client:
            interval, requests = 1, 0
            def universe(self):
                return universe()
            def candles(self, symbol, timeframe, count):
                calls.append(symbol)
                self.requests += 1
                return prices(symbol)
        result = collect(self.path, components=("daily",), client=Client(), workers=1, progress=lambda _: None)
        self.assertEqual(calls, ["FPT", "SHS", "ACV"])
        self.assertEqual(result["selected_symbols"], 3)
        self.assertEqual(result["unattempted"], 0)


class SignalHistoryTests(unittest.TestCase):
    def test_later_bar_preserves_event_but_same_bar_correction_retracts_it(self):
        repo = SqliteRepository(":memory:")
        self.addCleanup(repo.close)
        signal = Signal("FPT", DAY, SignalSide.BUY, "strategy", "cross", {}, "test", False,
                        datetime(2026, 9, 18, 10, tzinfo=VIETNAM), "5m", True, "HOSE")
        repo.save_signal(signal)
        repo.save_signal(replace(signal, side=SignalSide.NONE, closed_at=signal.closed_at + timedelta(minutes=5)))
        self.assertEqual(len(repo.signals_on(DAY, "strategy", "test", "5m", is_demo=False)), 1)
        repo.save_signal(replace(signal, side=SignalSide.NONE))
        self.assertEqual(repo.signals_on(DAY, "strategy", "test", "5m", is_demo=False), [])

    def test_preview_other_source_other_strategy_and_other_day_excluded(self):
        repo = SqliteRepository(":memory:")
        self.addCleanup(repo.close)
        base = Signal("FPT", DAY, SignalSide.BUY, "strategy", "cross", {}, "test", False,
                      datetime(2026, 9, 18, 10, tzinfo=VIETNAM), "5m", True, "HOSE")
        for signal in (replace(base, confirmed=False), replace(base, source="other"),
                       replace(base, strategy_id="other"), replace(base, session=date(2026, 9, 17))):
            repo.save_signal(signal)
        self.assertEqual(repo.signals_on(DAY, "strategy", "test", "5m", is_demo=False), [])


if __name__ == "__main__":
    unittest.main()
