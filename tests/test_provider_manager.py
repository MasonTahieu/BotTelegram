import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from fintech_bot.config import Settings, StrategySettings
from fintech_bot.data.base import CandleBatch, FetchMeta, FetchStatus, ProviderResult
from fintech_bot.data.provider_manager import DataProviderManager
from fintech_bot.data.replay import ReplayDataProvider
from fintech_bot.domain import FinancialSnapshot, Instrument, SignalSide, VIETNAM
from fintech_bot.market import MarketCalendar, ReplayClock
from fintech_bot.services.scanner import Scanner
from fintech_bot.storage.sqlite import SqliteRepository
from fintech_bot.strategies.ema_rsi import EmaRsiStrategy


class StubProvider:
    is_demo = False

    def __init__(self, source, series, instruments):
        self.source = source
        self.series = {key: list(value) for key, value in series.items()}
        self.instruments = instruments
        self.errors = {}
        self.status = {}
        self.calls = []
        self.financial = None

    def get_candles_batch(self, symbols, timeframes):
        self.calls.append((tuple(symbols), tuple(timeframes)))
        result = CandleBatch()
        for symbol in symbols:
            for timeframe in timeframes:
                pair = (symbol, timeframe)
                status = self.status.get(pair, FetchStatus.READY)
                if pair in self.errors:
                    result.errors[pair] = self.errors[pair]
                    result.meta[pair] = FetchMeta(self.source, None, FetchStatus.ERROR)
                elif pair in self.series:
                    result.candles[pair] = list(self.series[pair])
                    result.meta[pair] = FetchMeta(self.source, None, status, status is FetchStatus.STALE)
        return result

    def get_financials_result(self, symbol):
        return self.financial or ProviderResult(
            None, FetchMeta(self.source, None, FetchStatus.ERROR), "empty_response")


class ProviderManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = ReplayClock(datetime(2026, 9, 18, 10, tzinfo=VIETNAM))
        self.calendar = MarketCalendar()
        self.instruments = [Instrument("FPT", "HOSE")]
        replay = ReplayDataProvider(self.instruments, self.clock, self.calendar)
        self.series = {("FPT", "1d"): replay.get_candles("FPT", "1d")}
        self.primary = StubProvider("VNSTOCK_KBS", self.series, self.instruments)
        self.backup = StubProvider("VIETCAP_DIRECT", self.series, self.instruments)
        self.manager = DataProviderManager(
            self.primary, self.backup, self.calendar, self.instruments,
            required_bars=51, stale_after_seconds=1200, clock=self.clock,
        )

    def test_primary_ready_does_not_call_backup_and_keeps_actual_source(self):
        batch = self.manager.get_candles_batch(("FPT",), ("1d",))
        self.assertEqual(batch.meta["FPT", "1d"].source, "VNSTOCK_KBS")
        self.assertFalse(batch.meta["FPT", "1d"].fallback_used)
        self.assertFalse(self.backup.calls)

    def test_empty_invalid_or_stale_primary_uses_full_backup_series(self):
        for mode in ("empty", "invalid", "stale"):
            with self.subTest(mode=mode):
                self.primary.errors.clear()
                self.primary.status.clear()
                original = self.primary.series["FPT", "1d"]
                if mode == "empty":
                    self.primary.errors["FPT", "1d"] = "empty_response"
                elif mode == "invalid":
                    self.primary.series["FPT", "1d"] = original[:10]
                else:
                    self.primary.status["FPT", "1d"] = FetchStatus.STALE
                batch = self.manager.get_candles_batch(("FPT",), ("1d",))
                self.assertEqual(batch.candles["FPT", "1d"],
                                 [item for item in self.backup.series["FPT", "1d"] if item.is_closed])
                self.assertEqual(batch.meta["FPT", "1d"].source, "VIETCAP_DIRECT")
                self.assertTrue(batch.meta["FPT", "1d"].fallback_used)
                self.primary.series["FPT", "1d"] = original

    def test_both_fail_does_not_return_last_known_good(self):
        self.manager.get_candles_batch(("FPT",), ("1d",))
        self.primary.errors["FPT", "1d"] = "timeout"
        self.backup.errors["FPT", "1d"] = "rate_limit"
        batch = self.manager.get_candles_batch(("FPT",), ("1d",))
        self.assertNotIn(("FPT", "1d"), batch.candles)
        self.assertEqual(batch.meta["FPT", "1d"].status, FetchStatus.ERROR)

    def test_unready_symbol_is_skipped_but_counted_in_coverage(self):
        self.primary.errors["FPT", "1d"] = "timeout"
        self.backup.errors["FPT", "1d"] = "timeout"
        settings = Settings(("FPT",), self.root / "health.db", StrategySettings())
        repository = SqliteRepository(settings.database_path)
        self.addCleanup(repository.close)
        scanner = Scanner(self.manager, EmaRsiStrategy(settings.strategy), repository,
                          self.instruments, settings, self.clock, self.calendar)
        report = scanner.run()
        counts = self.manager.status_snapshot()["scan"]
        self.assertEqual((counts["historical"]["ERROR"], counts["scanner"]["SKIPPED"]), (1, 1))
        self.assertFalse(report.signals)
        self.assertFalse(report.errors)

    def test_no_recent_trade_is_counted_as_skipped_reason(self):
        self.primary.errors["FPT", "1d"] = "timeout"
        self.backup.series["FPT", "1d"] = self.series["FPT", "1d"][:-1]
        settings = Settings(("FPT",), self.root / "no-trade.db", StrategySettings())
        repository = SqliteRepository(settings.database_path)
        self.addCleanup(repository.close)
        scanner = Scanner(self.manager, EmaRsiStrategy(settings.strategy), repository,
                          self.instruments, settings, self.clock, self.calendar)
        report = scanner.run()
        counts = self.manager.status_snapshot()["scan"]["scanner"]
        self.assertIn("FPT", report.skipped)
        self.assertEqual(counts["SKIPPED_REASONS"], {"no_recent_trade": 1})
        self.assertEqual(counts["SKIPPED"], 1)

    def test_skipped_reason_counts_match_skipped_total(self):
        symbols = ("NO_TRADE", "MISSING", "INVALID", "SHORT", "OTHER")
        self.manager.begin_scan_cycle(symbols)
        self.manager._record_history("NO_TRADE", FetchStatus.READY)
        self.manager.record_scanner_outcome("NO_TRADE", eligible=False, reason="no_recent_trade")
        self.manager._record_history("MISSING", FetchStatus.MISSING)
        self.manager.record_scanner_outcome("MISSING", eligible=False)
        self.manager._record_history("INVALID", FetchStatus.INVALID)
        self.manager.record_scanner_outcome("INVALID", eligible=False)
        self.manager._record_history("SHORT", FetchStatus.ERROR, "Cần ít nhất 51 nến, hiện có 5.")
        self.manager.record_scanner_outcome("SHORT", eligible=False)
        self.manager.record_scanner_outcome("OTHER", eligible=False)
        counts = self.manager.finish_scan_cycle()["scanner"]
        self.assertEqual(counts["SKIPPED"], 5)
        self.assertEqual(counts["SKIPPED_REASONS"], {
            "no_recent_trade": 1, "historical_missing": 1, "historical_invalid": 1,
            "insufficient_history": 1, "other": 1,
        })

    def test_scanner_does_not_evaluate_stale_lkg_or_keep_pending_alert(self):
        settings = Settings(("FPT",), self.root / "app.db", StrategySettings(), timeframe="1d",
                            scan_interval_seconds=900, stale_after_seconds=1200)
        repository = SqliteRepository(settings.database_path)
        self.addCleanup(repository.close)
        scanner = Scanner(self.manager, EmaRsiStrategy(settings.strategy), repository,
                          self.instruments, settings, self.clock, self.calendar)
        first = scanner.run(["FPT"], ("1d",))
        signal = replace(first.signals[0], side=SignalSide.BUY)
        repository.save_signal(signal)
        repository.enqueue(signal, "alice", self.clock.now().timestamp(), self.clock.now().timestamp() + 60)
        self.primary.errors["FPT", "1d"] = "timeout"
        self.backup.errors["FPT", "1d"] = "timeout"
        report = scanner.run(["FPT"], ("1d",))
        self.assertFalse(report.signals)
        self.assertIn("Historical ERROR", report.skipped["FPT"])
        self.assertFalse(report.errors)
        self.assertEqual(repository.pending_count(), 0)

    def test_financial_primary_failure_falls_back_with_actual_source(self):
        snapshot = FinancialSnapshot("FPT", "2026-Q2", self.clock.now().date(), roe=.2)
        self.primary.financial = ProviderResult(
            None, FetchMeta("VNSTOCK_KBS", self.clock.now(), FetchStatus.ERROR),
            "unsupported_capability",
        )
        self.backup.financial = ProviderResult(
            snapshot, FetchMeta("VIETCAP_DIRECT", self.clock.now(), FetchStatus.READY))
        result = self.manager.get_financials_result("FPT")
        self.assertEqual(result.value, snapshot)
        self.assertEqual(result.meta.source, "VIETCAP_DIRECT")
        self.assertTrue(result.meta.fallback_used)

    def _quote_result(self, reference=None):
        history = self.series["FPT", "1d"]
        current = history[-1]
        previous = next(item for item in reversed(history[:-1]) if item.is_closed)
        envelope = {"schema": 1, "fetched_at": self.clock.now().isoformat(), "data": {
            "listingInfo": {"symbol": "FPT", "board": "HSX", "stockType": "STOCK",
                            "isDelisted": 0, "tradingStatus": "TRADING_ACTIVATED",
                            "tradingDate": current.session.isoformat(),
                            "refPrice": previous.close if reference is None else reference},
            "matchPrice": {"symbol": "FPT"}, "bidAsk": {"symbol": "FPT"},
        }}
        return ProviderResult((current, envelope),
                              FetchMeta("VIETCAP_DIRECT", self.clock.now(), FetchStatus.READY))

    def test_live_quote_is_never_read_or_merged_into_history(self):
        self.backup.get_quote_result = lambda *args: self.fail("scanner must not read live quote")
        batch = self.manager.get_candles_batch(("FPT",), ("1d",))
        self.assertEqual(batch.meta["FPT", "1d"].status, FetchStatus.READY)
        self.assertTrue(all(item.is_closed for item in batch.candles["FPT", "1d"]))
        self.assertNotIn("+", batch.meta["FPT", "1d"].source)

    def test_status_is_replaced_per_scan_not_accumulated(self):
        for _ in range(2):
            self.manager.begin_scan_cycle(("FPT",))
            batch = self.manager.get_candles_batch(("FPT",), ("1d",))
            self.manager.record_scanner_outcome("FPT", SignalSide.NONE)
            snapshot = self.manager.finish_scan_cycle()
            self.assertEqual(snapshot["historical"]["READY"], 1)
            self.assertEqual(snapshot["scanner"]["ELIGIBLE"], 1)


if __name__ == "__main__":
    unittest.main()
