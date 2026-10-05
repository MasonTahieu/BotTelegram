"""Financial download recovery without changing historical collection behavior."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fintech_bot.data.vietcap_http import VietcapClient, SourceUnavailable, atomic_json, read_json
from fintech_bot.data.vietcap_sync import collect
from tests.test_vietcap import envelope, financials


class FinancialCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def client(self, count, behavior):
        class Client:
            interval, requests = 2, 0
            def universe(self):
                return envelope([{"symbol": f"T{i:02}", "board": "HSX", "type": "STOCK"}
                                 for i in range(count)])
            def financials(self, symbol, component):
                self.requests += 1
                return behavior(symbol, component)
            def candles(self, symbol, timeframe, count):
                self.requests += 1
                return behavior(symbol, "daily")
        return Client()

    def test_iq_context_is_accepted_and_market_headers_are_preserved(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            if (request.get_header("Origin") != "https://trading.vietcap.com.vn" or
                    request.get_header("Referer") != "https://trading.vietcap.com.vn/"):
                raise urllib.error.HTTPError(request.full_url, 403, "missing context", {}, None)
            return io.BytesIO(json.dumps({"data": []}).encode())
        client = VietcapClient(opener=opener, sleep=lambda _: None)
        client.financials("VIC", "ratios")
        client.candles("BWE", "1d", 7)
        self.assertEqual(len(seen), 2)
        for request in seen:
            self.assertEqual(request.get_header("User-agent"),
                             "FintechBot/0.4 (local academic market-data project)")
            self.assertIsNone(request.get_header("Authorization"))

    def test_financial_network_errors_pause_then_reach_remaining_symbol(self):
        cached = financials("T00")[0]
        atomic_json(self.directory / "T00-ratios.json", cached)
        def behavior(symbol, component):
            if symbol != "T10":
                raise TimeoutError("The read operation timed out")
            return financials(symbol)[0]
        with patch("fintech_bot.data.vietcap_sync.time.sleep") as sleep:
            report = collect(self.directory, components=("ratios",), workers=1,
                             client=self.client(11, behavior), progress=lambda _: None)
        self.assertIsNone(report["stopped"])
        self.assertEqual(report["unattempted"], 0)
        self.assertEqual(report["downloaded"], 1)
        self.assertEqual(report["network_pauses"], 1)
        sleep.assert_called_once_with(30)
        self.assertEqual(read_json(self.directory / "T00-ratios.json"), cached)
        self.assertTrue((self.directory / "T10-ratios.json").exists())

    def test_missing_financial_tables_do_not_stop_other_symbols(self):
        def behavior(symbol, component):
            return financials(symbol)[0] if symbol == "T10" else envelope({"data": []})
        with patch("fintech_bot.data.vietcap_sync.time.sleep") as sleep:
            report = collect(self.directory, components=("ratios",), workers=1,
                             client=self.client(11, behavior), progress=lambda _: None)
        self.assertIsNone(report["stopped"])
        self.assertEqual(report["unattempted"], 0)
        self.assertEqual(len(report["errors"]), 10)
        self.assertEqual(report["downloaded"], 1)
        sleep.assert_not_called()

    def test_persistent_financial_network_failure_has_bounded_pauses(self):
        def behavior(symbol, component):
            raise TimeoutError("The read operation timed out")
        with patch("fintech_bot.data.vietcap_sync.time.sleep") as sleep:
            report = collect(self.directory, components=("ratios",), workers=1,
                             client=self.client(31, behavior), progress=lambda _: None)
        self.assertIn("timed out", report["stopped"])
        self.assertEqual(report["network_pauses"], 2)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(report["errors"]), 30)
        self.assertEqual(report["unattempted"], 1)

    def test_access_denial_still_stops_financial_collection(self):
        def behavior(symbol, component):
            raise SourceUnavailable("HTTP 403", status=403)
        with patch("fintech_bot.data.vietcap_sync.time.sleep") as sleep:
            report = collect(self.directory, components=("ratios",), workers=1,
                             client=self.client(11, behavior), progress=lambda _: None)
        self.assertEqual(report["stopped"], "HTTP 403")
        self.assertEqual(report["unattempted"], 10)
        sleep.assert_not_called()

    def test_daily_collection_keeps_its_existing_stop_policy(self):
        def behavior(symbol, component):
            raise TimeoutError("The read operation timed out")
        with patch("fintech_bot.data.vietcap_sync.time.sleep") as sleep:
            report = collect(self.directory, components=("daily",), workers=1,
                             client=self.client(11, behavior), progress=lambda _: None)
        self.assertIn("timed out", report["stopped"])
        self.assertEqual(len(report["errors"]), 10)
        self.assertEqual(report["unattempted"], 1)
        self.assertEqual(report["network_pauses"], 0)
        sleep.assert_not_called()

    def test_financial_resume_reuses_success_and_retries_failed_component(self):
        def behavior(symbol, component):
            return financials(symbol)[0]
        first = collect(self.directory, components=("ratios",), workers=1,
                        client=self.client(2, behavior), progress=lambda _: None)
        self.assertEqual(first["downloaded"], 2)
        atomic_json(self.directory / "failures.json", {"T01-ratios": {"error": "old timeout"}})
        retry_client = self.client(2, behavior)
        resumed = collect(self.directory, components=("ratios",), workers=1,
                          client=retry_client, resume=True, max_age=10**9,
                          progress=lambda _: None)
        self.assertEqual((resumed["reused"], resumed["downloaded"]), (1, 1))
        self.assertEqual(retry_client.requests, 1)
        self.assertEqual(read_json(self.directory / "failures.json"), {})


if __name__ == "__main__":
    unittest.main()
