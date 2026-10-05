"""Vnstock 4.0.8/KBS adapter with normalized, atomic local caches."""

import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from fintech_bot.data.base import CandleBatch, FetchMeta, FetchStatus, ProviderResult
from fintech_bot.data.readiness import DataNotReady, DataUnavailable, InvalidData
from fintech_bot.data.validation import validate_candles, validate_market_times
from fintech_bot.data.vietcap_http import atomic_json, read_json
from fintech_bot.domain import Candle, FinancialSnapshot, Instrument, VIETNAM

logger = logging.getLogger(__name__)

VNSTOCK_KBS = "VNSTOCK_KBS"


def _now() -> datetime:
    return datetime.now(VIETNAM)


def _date_value(value) -> date:
    parsed = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if isinstance(parsed, date) and not isinstance(parsed, datetime):
        return parsed
    if not isinstance(parsed, datetime):
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(VIETNAM)
    return parsed.date()


class VnstockProvider:
    """Only talks to Vnstock's KBS source; VCI is never selected implicitly."""

    source = VNSTOCK_KBS
    is_demo = False
    display_name = "Vnstock / KBS"

    def __init__(self, directory: Path, calendar, *, instruments=(), required_bars=51,
                 stale_after_seconds=1200, network_batch_limit=5, clock=None,
                 max_api_calls_per_minute=9, periodic_refresh_seconds=900,
                 network_calls_per_batch=1):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.calendar = calendar
        self.required_bars = required_bars
        self.stale_after_seconds = stale_after_seconds
        self.network_batch_limit = max(1, int(network_batch_limit))
        self.periodic_refresh_seconds = max(1, int(periodic_refresh_seconds))
        self.network_calls_per_batch = max(1, int(network_calls_per_batch))
        self.clock = clock
        # Guest accounts are limited to 20 internal requests/minute. One public
        # OHLCV call can consume more than one internal quota unit, so cap the
        # adapter at nine logical calls and fail over before upstream sys.exit().
        self.max_api_calls_per_minute = max(1, int(max_api_calls_per_minute))
        self._api_call_times = deque()
        self.api_call_count = 0
        self._api_call_lock = threading.Lock()
        self._cursor = 0
        self._refresh_window_started = 0.0
        self._refresh_window_remaining = self.network_batch_limit
        self._fallback_instruments = list(instruments)
        cached = self._read_universe_cache()
        self.instruments = cached or list(instruments)
        self.exchanges = {item.symbol: item.exchange for item in self.instruments}

    def _current_time(self) -> datetime:
        return self.clock.now() if self.clock is not None else _now()

    def _reserve_api_call(self):
        now = time.monotonic()
        with self._api_call_lock:
            while self._api_call_times and now - self._api_call_times[0] >= 60:
                self._api_call_times.popleft()
            if len(self._api_call_times) >= self.max_api_calls_per_minute:
                retry_after = max(1, math.ceil(60 - (now - self._api_call_times[0])))
                raise DataUnavailable(f"rate_limit: thử lại sau {retry_after}s.")
            self._api_call_times.append(now)
            self.api_call_count += 1

    @staticmethod
    def _api():
        # Lazy import keeps demo/CSV deployments independent of Vnstock.
        try:
            from vnstock import Fundamental, Market, Reference
        except ImportError as error:
            raise DataUnavailable("Thiếu dependency vnstock==4.0.8.") from error
        return Reference, Market, Fundamental

    @property
    def universe_path(self) -> Path:
        return self.directory / "universe.json"

    def _read_universe_cache(self):
        if not self.universe_path.exists():
            return []
        try:
            envelope = read_json(self.universe_path)
            if envelope.get("source") != self.source or envelope.get("schema") != 1:
                raise ValueError("Sai schema/source.")
            result = [Instrument(**row) for row in envelope["instruments"]]
            if not result:
                raise ValueError("Universe rỗng.")
            return result
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("Ignoring invalid Vnstock universe cache: %s", error)
            return []

    def refresh_universe(self) -> list[Instrument]:
        Reference, _, _ = self._api()
        try:
            self._reserve_api_call()
            frame = Reference().equity().list_by_exchange(source="kbs")
            required = {"symbol", "organ_name", "exchange", "type"}
            if frame is None or not required.issubset(frame.columns):
                raise ValueError("KBS universe sai schema.")
            rows = {}
            for row in frame.to_dict("records"):
                symbol = str(row.get("symbol", "")).strip().upper()
                exchange = str(row.get("exchange", "")).strip().upper()
                asset_type = str(row.get("type", "")).strip().lower()
                if not symbol or exchange not in {"HOSE", "HNX", "UPCOM"} or asset_type != "stock":
                    continue
                rows[symbol] = Instrument(symbol, exchange, str(row.get("organ_name") or ""), "stock", "active")
            if not rows:
                raise ValueError("KBS universe không có cổ phiếu hợp lệ.")
            result = sorted(rows.values(), key=lambda item: (item.exchange, item.symbol))
            atomic_json(self.universe_path, {
                "schema": 1, "source": self.source, "fetched_at": self._current_time().isoformat(),
                "instruments": [asdict(item) for item in result],
            })
            self.instruments = result
            self.exchanges = {item.symbol: item.exchange for item in result}
            return list(result)
        except SystemExit as error:
            if self.instruments:
                logger.warning("KBS universe rate limited; preserving cached/fallback universe")
                return list(self.instruments)
            raise DataUnavailable("rate_limit: không tải được universe KBS.") from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            if self.instruments:
                logger.warning("KBS universe unavailable; preserving cached/fallback universe: %s", error)
                return list(self.instruments)
            raise DataUnavailable("Không tải được universe KBS.") from error

    def ensure_universe(self) -> list[Instrument]:
        if self.instruments and self.universe_path.exists():
            return list(self.instruments)
        return self.refresh_universe()

    def _path(self, symbol, timeframe):
        if timeframe != "1d":
            raise ValueError("Vnstock runtime chỉ hỗ trợ khung nến 1d.")
        return self.directory / f"{symbol}-daily.json"

    @staticmethod
    def _candle_payload(candle):
        payload = asdict(candle)
        payload["session"] = candle.session.isoformat()
        payload["closed_at"] = candle.closed_at.isoformat() if candle.closed_at else None
        payload["observed_at"] = candle.observed_at.isoformat() if candle.observed_at else None
        return payload

    @staticmethod
    def _read_candle(payload):
        values = dict(payload)
        values["session"] = date.fromisoformat(values["session"])
        for name in ("closed_at", "observed_at"):
            values[name] = datetime.fromisoformat(values[name]) if values.get(name) else None
        return Candle(**values)

    def _write_cache(self, symbol, timeframe, fetched, candles):
        atomic_json(self._path(symbol, timeframe), {
            "schema": 1, "source": self.source, "symbol": symbol, "timeframe": timeframe,
            "fetched_at": fetched.isoformat(),
            "candles": [self._candle_payload(item) for item in candles],
        })

    def get_cached_candles(self, symbol, timeframe="1d"):
        if timeframe != "1d":
            raise ValueError("Vnstock runtime chỉ hỗ trợ khung nến 1d.")
        path = self._path(symbol, timeframe)
        if not path.exists():
            raise DataNotReady("Chưa có cache Vnstock/KBS cho mã yêu cầu.")
        try:
            envelope = read_json(path)
            if (envelope.get("schema") != 1 or envelope.get("source") != self.source
                    or envelope.get("symbol") != symbol or envelope.get("timeframe") != timeframe):
                raise ValueError("Sai schema/source/mã/khung.")
            fetched = datetime.fromisoformat(envelope["fetched_at"])
            if fetched.utcoffset() is None:
                raise ValueError("Thời gian cache thiếu múi giờ.")
            candles = [self._read_candle(item) for item in envelope["candles"]]
            validate_candles(symbol, candles, 1)
            exchange = self.exchanges.get(symbol)
            if exchange:
                validate_market_times(candles, self.calendar, exchange)
            return candles, fetched.astimezone(VIETNAM)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InvalidData("Cache Vnstock/KBS không vượt qua kiểm tra dữ liệu.") from error

    def _normalize_ohlcv(self, frame, symbol, timeframe, fetched):
        if timeframe != "1d":
            raise ValueError("Vnstock runtime chỉ hỗ trợ khung nến 1d.")
        required = {"time", "open", "high", "low", "close", "volume"}
        if frame is None or not required.issubset(frame.columns) or frame.empty:
            raise InvalidData("KBS trả empty response hoặc sai schema OHLCV.")
        exchange = self.exchanges.get(symbol)
        if exchange is None:
            raise InvalidData("KBS universe chưa xác nhận mã yêu cầu.")
        result = []
        for row in frame.to_dict("records"):
            session = _date_value(row["time"])
            ends = self.calendar.ends(session, exchange, timeframe)
            if not ends:
                raise InvalidData("KBS trả mốc ngoài lịch giao dịch.")
            end = ends[0]
            closed = fetched >= end + timedelta(seconds=60)
            values = []
            for name in ("open", "high", "low", "close"):
                value = float(row[name]) * 1000.0  # Vnstock/KBS OHLCV contract is thousand VND.
                if not math.isfinite(value):
                    raise InvalidData("KBS trả giá không hữu hạn.")
                values.append(value)
            volume = row["volume"]
            if isinstance(volume, bool) or not float(volume).is_integer():
                raise InvalidData("KBS trả volume không phải số cổ phiếu nguyên.")
            result.append(Candle(symbol, session, *values, int(volume), end, timeframe, closed,
                                 None if closed else fetched))
        validate_candles(symbol, result, 1)
        validate_market_times(result, self.calendar, exchange)
        return result

    def _fetch_candles(self, symbol, timeframe, *, start=None, end=None, persist=True):
        if timeframe != "1d":
            raise ValueError("Vnstock runtime chỉ hỗ trợ khung nến 1d.")
        _, Market, _ = self._api()
        fetched = self._current_time()
        start = start or fetched.date() - timedelta(days=800)
        bounded_end = end
        end = end or fetched.date()
        try:
            self._reserve_api_call()
            frame = Market().equity(symbol).ohlcv(
                start=start.isoformat(), end=end.isoformat(),
                interval="1D", source="kbs",
            )
            candles = self._normalize_ohlcv(frame, symbol, timeframe, fetched)
            if bounded_end is not None:
                candles = [candle for candle in candles if candle.session <= bounded_end]
            validate_candles(symbol, candles, self.required_bars if persist else 1)
            if persist:
                self._write_cache(symbol, timeframe, fetched, candles)
            return candles, fetched
        except SystemExit as error:
            raise DataUnavailable(f"rate_limit: KBS tạm từ chối {symbol}/{timeframe}.") from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DataUnavailable(f"KBS không cung cấp {symbol}/{timeframe} hợp lệ.") from error

    def get_history_through(self, symbol, timeframe, end, *, start=None):
        candles, _ = self._fetch_candles(symbol, timeframe, start=start, end=end,
                                          persist=start is None)
        return candles

    def get_candles(self, symbol, timeframe="1d"):
        candles, _ = self._fetch_candles(symbol, timeframe)
        return candles

    def get_historical_candles(self, symbol, timeframe="1d"):
        return self.get_candles(symbol, timeframe)

    def get_candles_batch(self, symbols, timeframes):
        pairs = [(symbol, timeframe) for symbol in symbols for timeframe in timeframes]
        result = CandleBatch()
        refresh = set(pairs)
        if len(pairs) > self.network_calls_per_batch:
            # Periodic scans refresh only an already bootstrapped KBS cache.
            # Missing whole-market history belongs to the separate resumable job.
            now = time.monotonic()
            if now - self._refresh_window_started >= self.periodic_refresh_seconds:
                self._refresh_window_started = now
                self._refresh_window_remaining = self.network_batch_limit
            existing = [pair for pair in pairs if self._path(*pair).exists()]
            offset = self._cursor % len(existing) if existing else 0
            rotated = existing[offset:] + existing[:offset]
            allowance = min(self.network_calls_per_batch, self._refresh_window_remaining)
            refresh = set(rotated[:allowance])
            self._refresh_window_remaining -= len(refresh)
            self._cursor = ((offset + allowance) % len(existing)
                            if existing else 0)
        for pair in pairs:
            symbol, timeframe = pair
            try:
                if pair in refresh:
                    candles, fetched = self._fetch_candles(symbol, timeframe)
                else:
                    candles, fetched = self.get_cached_candles(symbol, timeframe)
                result.candles[pair] = candles
                result.meta[pair] = FetchMeta(self.source, fetched, FetchStatus.READY)
            except (OSError, ValueError, KeyError, TypeError) as error:
                result.errors[pair] = str(error)
                result.meta[pair] = FetchMeta(self.source, None, FetchStatus.ERROR)
        return result

    def get_quote(self, symbol):
        _, Market, _ = self._api()
        try:
            self._reserve_api_call()
            frame = Market().equity(symbol).quote(source="kbs")
            if frame is None or frame.empty or "symbol" not in frame.columns:
                raise ValueError("Empty quote.")
            row = frame.iloc[0].to_dict()
            if row.get("symbol") != symbol:
                raise ValueError("Quote sai mã.")
            return row
        except SystemExit as error:
            raise DataUnavailable("rate_limit: KBS quote tạm bị giới hạn.") from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DataUnavailable("KBS quote chưa sẵn sàng.") from error

    def refresh_quotes(self, symbols):
        return {symbol: self.get_quote(symbol) for symbol in symbols}

    def get_financials_result(self, symbol):
        """KBS v4.0.8 ratios lack a trustworthy publication date.

        The project contract forbids substituting a period-end date. Mark this
        primary result insufficient so the manager can use Vietcap IQ.
        """
        fetched = self._current_time()
        # The mandatory 4.0.8 contract probe established that the KBS ratios and
        # income frames expose periods but no trustworthy publication date. Do
        # not spend two API calls per symbol only to rediscover that limitation.
        reason = "unsupported_capability: KBS không có ngày công bố đáng tin cậy."
        return ProviderResult(None, FetchMeta(self.source, fetched, FetchStatus.ERROR), reason)

    def get_financials(self, symbol) -> FinancialSnapshot | None:
        result = self.get_financials_result(symbol)
        if result.meta.status is not FetchStatus.READY or result.value is None:
            raise DataUnavailable(result.error or "Tài chính KBS chưa sẵn sàng.")
        return result.value

    def status_hint(self, symbol, timeframe=None):
        return ""
