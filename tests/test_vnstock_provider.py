import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from fintech_bot.data.base import FetchStatus
from fintech_bot.data.readiness import DataUnavailable
from fintech_bot.data.vnstock_provider import VnstockProvider
from fintech_bot.domain import Instrument, VIETNAM
from fintech_bot.market import MarketCalendar, ReplayClock


class VnstockProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = ReplayClock(datetime(2026, 9, 18, 16, tzinfo=VIETNAM))
        self.provider = VnstockProvider(
            self.root, MarketCalendar(), instruments=[Instrument("FPT", "HOSE")],
            required_bars=2, stale_after_seconds=1200, clock=self.clock,
        )

    @staticmethod
    def daily_frame():
        return pd.DataFrame([
            {"time": "2026-09-17 07:00:00", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "volume": 1000},
            {"time": "2026-09-18 07:00:00", "open": 101.0, "high": 102.0,
             "low": 100.0, "close": 101.5, "volume": 2000},
        ])

    def test_daily_normalization_uses_vnd_timezone_and_closed_session(self):
        candles = self.provider._normalize_ohlcv(
            self.daily_frame(), "FPT", "1d", self.clock.now())
        self.assertEqual(candles[-1].close, 101_500)
        self.assertEqual(candles[-1].volume, 2000)
        self.assertEqual(candles[-1].closed_at.hour, 14)
        self.assertTrue(candles[-1].is_closed)
        self.assertIsNotNone(candles[-1].closed_at.utcoffset())

    def test_valid_cache_roundtrip_and_invalid_payload_does_not_replace_it(self):
        candles = self.provider._normalize_ohlcv(
            self.daily_frame(), "FPT", "1d", self.clock.now())
        self.provider._write_cache("FPT", "1d", self.clock.now(), candles)
        loaded, fetched = self.provider.get_cached_candles("FPT", "1d")
        self.assertEqual(loaded, candles)
        self.assertEqual(fetched, self.clock.now())
        with patch.object(self.provider, "_normalize_ohlcv", side_effect=ValueError("invalid")):
            with self.assertRaises(ValueError):
                self.provider._normalize_ohlcv(None, "FPT", "1d", self.clock.now())
        self.assertEqual(self.provider.get_cached_candles("FPT", "1d")[0], candles)

    def test_batch_metadata_and_bounded_cache_path(self):
        candles = self.provider._normalize_ohlcv(
            self.daily_frame(), "FPT", "1d", self.clock.now())
        self.provider._write_cache("FPT", "1d", self.clock.now(), candles)
        with patch.object(self.provider, "_fetch_candles", return_value=(candles, self.clock.now())):
            batch = self.provider.get_candles_batch(("FPT",), ("1d",))
        self.assertEqual(batch.meta["FPT", "1d"].source, "VNSTOCK_KBS")
        self.assertEqual(batch.meta["FPT", "1d"].status, FetchStatus.READY)

    def test_incremental_history_uses_bounded_dates_without_replacing_full_cache(self):
        candles = self.provider._normalize_ohlcv(
            self.daily_frame(), "FPT", "1d", self.clock.now())
        self.provider._write_cache("FPT", "1d", self.clock.now(), candles)
        requests = []
        class Market:
            def equity(self, symbol):
                return self
            def ohlcv(self, **kwargs):
                requests.append(kwargs)
                return VnstockProviderTests.daily_frame().tail(1)
        with patch.object(self.provider, "_api", return_value=(None, Market, None)):
            delta = self.provider.get_history_through(
                "FPT", "1d", date(2026, 9, 18), start=date(2026, 9, 17))
        self.assertEqual(len(delta), 1)
        self.assertEqual((requests[0]["start"], requests[0]["end"]),
                         ("2026-09-17", "2026-09-18"))
        self.assertEqual(self.provider.get_cached_candles("FPT", "1d")[0], candles)

    def test_financials_fail_closed_without_inventing_publication_date(self):
        result = self.provider.get_financials_result("FPT")
        self.assertIsNone(result.value)
        self.assertEqual(result.meta.status, FetchStatus.ERROR)
        self.assertIn("ngày công bố", result.error)

    def test_local_rate_guard_fails_fast_before_upstream_limit(self):
        provider = VnstockProvider(
            self.root, MarketCalendar(), instruments=[Instrument("FPT", "HOSE")],
            required_bars=2, max_api_calls_per_minute=1,
        )
        provider._reserve_api_call()
        with self.assertRaisesRegex(DataUnavailable, "rate_limit"):
            provider._reserve_api_call()

    def test_upstream_system_exit_becomes_failover_eligible_error(self):
        class Market:
            def equity(self, symbol):
                return self

            def ohlcv(self, **kwargs):
                raise SystemExit("upstream 429")

        with patch.object(self.provider, "_api", return_value=(None, Market, None)):
            with self.assertRaisesRegex(DataUnavailable, "rate_limit"):
                self.provider.get_candles("FPT", "1d")

    def test_universe_system_exit_preserves_fallback_universe(self):
        class Reference:
            def equity(self):
                return self

            def list_by_exchange(self, **kwargs):
                raise SystemExit("upstream 429")

        with patch.object(self.provider, "_api", return_value=(Reference, None, None)):
            self.assertEqual(self.provider.refresh_universe(), [Instrument("FPT", "HOSE")])


if __name__ == "__main__":
    unittest.main()
