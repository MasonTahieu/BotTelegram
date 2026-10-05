"""Small public-data client. No trading, account, cookie or Vnstock dependency."""

import json
import math
import os
import re
import time
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from fintech_bot.domain import VIETNAM

TRADING = "https://trading.vietcap.com.vn/api/"
INSIGHT = "https://iq.vietcap.com.vn/api/iq-insight-service/v1/company/"


class SourceUnavailable(ValueError):
    """Retry only in a later, explicitly started collection run."""

    def __init__(self, message, *, retry_after=0, status=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=path.name, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def check_symbol(symbol: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]{2,10}", symbol):
        raise ValueError("Mã cổ phiếu không hợp lệ.")
    return symbol


class VietcapClient:
    def __init__(self, interval=1.0, timeout=15, opener=None, sleep=time.sleep, monotonic=time.monotonic):
        if not math.isfinite(interval) or interval < .5:
            raise ValueError("Khoảng cách yêu cầu phải từ 0.5 giây trở lên.")
        self.interval, self.timeout = interval, timeout
        self.opener = opener or urllib.request.urlopen
        self.sleep, self.monotonic = sleep, monotonic
        self.next_request = 0.0
        self.blocked = None
        self.blocked_until = 0.0
        self.blocked_status = None
        self.requests = 0
        self._lock = threading.Lock()

    def _request(self, url, body=None):
        if self.blocked and self.blocked_until and self.monotonic() >= self.blocked_until:
            self.blocked, self.blocked_until, self.blocked_status = None, 0.0, None
        if self.blocked:
            remaining = max(0, int(self.blocked_until - self.monotonic())) if self.blocked_until else 0
            raise SourceUnavailable(self.blocked, retry_after=remaining, status=self.blocked_status)
        for attempt in range(2):
            with self._lock:
                self.sleep(max(0, self.next_request - self.monotonic()))
                if self.blocked:
                    remaining = max(0, int(self.blocked_until - self.monotonic())) if self.blocked_until else 0
                    raise SourceUnavailable(self.blocked, retry_after=remaining, status=self.blocked_status)
                self.next_request = self.monotonic() + self.interval
                self.requests += 1
            headers = {"User-Agent": "FintechBot/0.4 (local academic market-data project)",
                       "Accept": "application/json", "Content-Type": "application/json"}
            if url.startswith((TRADING, INSIGHT)):
                # Market data and IQ financial data require the trading site's request context.
                headers.update({"Origin": "https://trading.vietcap.com.vn",
                                "Referer": "https://trading.vietcap.com.vn/"})
            request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                             headers=headers)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(8_000_001)
                    if len(raw) > 8_000_000:
                        raise ValueError("Phản hồi vượt giới hạn 8 MB.")
                    result = json.loads(raw)
                if isinstance(result, dict) and result.get("successful") is False:
                    raise ValueError("Nguồn báo yêu cầu không thành công.")
                return {"schema": 1, "fetched_at": datetime.now(VIETNAM).isoformat(), "data": result}
            except urllib.error.HTTPError as error:
                if error.code in {401, 403, 429}:
                    raw_retry = error.headers.get("Retry-After")
                    try:
                        retry_after = max(0, min(86400, int(raw_retry)))
                    except (TypeError, ValueError):
                        retry_after = 0
                    shown_retry = raw_retry if raw_retry is not None else "không được cung cấp"
                    self.blocked = f"Nguồn trả HTTP {error.code}; dừng thu thập. Retry-After: {shown_retry}."
                    self.blocked_status = error.code
                    effective_retry = (retry_after or 60) if error.code == 429 else 0
                    self.blocked_until = self.monotonic() + effective_retry if effective_retry else 0.0
                    raise SourceUnavailable(self.blocked, retry_after=effective_retry, status=error.code) from error
                if error.code < 500 or attempt:
                    raise ValueError(f"Nguồn trả HTTP {error.code}.") from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt:
                    raise ValueError(f"Không kết nối được nguồn: {error}") from error
            self.sleep(2)
        raise AssertionError("unreachable")

    def universe(self):
        return self._request(TRADING + "price/symbols/getAll")

    def quotes(self, symbols):
        symbols = list(dict.fromkeys(check_symbol(symbol) for symbol in symbols))
        if not 1 <= len(symbols) <= 100:
            raise ValueError("Mỗi lô bảng giá cần 1–100 mã.")
        return self._request(TRADING + "price/symbols/getList", {"symbols": symbols})

    def candles(self, symbol, timeframe, count, *, to_timestamp=None):
        check_symbol(symbol)
        if timeframe != "1d" or not 1 <= count <= 5000:
            raise ValueError("Khung hoặc số lượng nến không hợp lệ.")
        envelope = self._request(TRADING + "chart/OHLCChart/gap-chart", {
            "symbols": [symbol], "timeFrame": "ONE_DAY",
            "to": int(time.time() if to_timestamp is None else to_timestamp), "countBack": count,
        })
        envelope["requested_bars"] = count
        return envelope

    def financials(self, symbol, section):
        check_symbol(symbol)
        suffix = {"ratios": "statistics-financial", "income": "financial-statement?section=INCOME_STATEMENT"}[section]
        return self._request(INSIGHT + symbol + "/" + suffix)
