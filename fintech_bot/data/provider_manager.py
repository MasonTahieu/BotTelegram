"""Select one complete closed-daily series per symbol.

Live quotes are deliberately outside this adapter. Mixing a current quote from
one vendor into another vendor's adjusted history makes EMA/RSI results hard to
reproduce and can corrupt the historical cache.
"""

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime

from fintech_bot.data.base import CandleBatch, FetchMeta, FetchStatus, ProviderResult
from fintech_bot.data.daily_cache import LocalDailyCache
from fintech_bot.data.readiness import DataNotReady, DataUnavailable, InvalidData, StaleData
from fintech_bot.data.validation import DataValidationError, validate_candles, validate_market_times
from fintech_bot.domain import VIETNAM


def failure_category(error) -> str:
    """Small, stable error taxonomy used by status/bootstrap reports."""
    category = getattr(error, "category", None)
    if category:
        return category
    if isinstance(error, StaleData):
        return "stale_history"
    if isinstance(error, DataNotReady):
        return "missing_cache"
    if isinstance(error, InvalidData):
        return "invalid_data"
    text = str(error).lower()
    if "429" in text or "rate limit" in text or "rate_limit" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "stale" in text or "dừng ở" in text:
        return "stale_history"
    if "gaierror" in text or "dns" in text or "name resolution" in text:
        return "network_dns"
    if "connection" in text or "network" in text or "kết nối" in text:
        return "network_error"
    if "recent session" in text or "phiên gần nhất" in text:
        return "missing_recent_session"
    if "ohlc" in text:
        return "invalid_ohlcv"
    if "empty" in text or "rỗng" in text:
        return "empty_response"
    if "chưa xác nhận mã" in text:
        return "source_unconfirmed"
    if "schema" in text or "định dạng" in text:
        return "invalid_schema"
    if "đủ" in text or "ít nhất" in text or "at least" in text:
        return "insufficient_history"
    if "unsupported" in text or "không hỗ trợ" in text:
        return "unsupported_capability"
    return "provider_exception"


def _error_status(error):
    if isinstance(error, DataNotReady):
        return FetchStatus.MISSING
    if isinstance(error, StaleData):
        return FetchStatus.STALE
    if isinstance(error, (InvalidData, DataValidationError)):
        return FetchStatus.INVALID
    return FetchStatus.ERROR


class DataProviderManager:
    """Read canonical history, then KBS cache, then Vietcap history cache.

    Runtime scans never perform network collection and never merge candles from
    different providers. The preparation command owns network/bootstrap work.
    """

    source = "MULTI_PROVIDER"
    display_name = "Closed daily history (KBS, Vietcap fallback)"
    is_demo = False

    def __init__(self, primary, backup, calendar, instruments, *, required_bars,
                 stale_after_seconds, clock=None, enable_failover=True, cache_directory=None):
        self.primary, self.backup = primary, backup
        self.calendar = calendar
        self.required_bars = max(52, int(required_bars))
        # Retained for configuration compatibility; daily freshness is session based.
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self.enable_failover = enable_failover
        self.daily_cache = LocalDailyCache(cache_directory) if cache_directory else None
        merged = {}
        for item in [*getattr(backup, "instruments", []),
                     *getattr(primary, "instruments", []), *instruments]:
            merged[item.symbol] = item
        self.instruments = sorted(merged.values(), key=lambda item: (item.exchange, item.symbol))
        self.exchanges = {item.symbol: item.exchange for item in self.instruments}
        self._last_meta = {}
        self._active_scan = None
        self._last_scan = None

    def _now(self):
        return self.clock.now() if self.clock is not None else datetime.now(VIETNAM)

    @staticmethod
    def _blank_counts(names):
        return {name: 0 for name in names}

    def begin_scan_cycle(self, symbols):
        self._active_scan = {
            "started_at": self._now(),
            "requested": tuple(dict.fromkeys(symbols)),
            "historical": {}, "scanner": {},
            "failure_groups": defaultdict(list),
        }

    def _record_history(self, symbol, status, detail="", source=""):
        if self._active_scan is None:
            return
        value = status.value if isinstance(status, FetchStatus) else str(status)
        self._active_scan["historical"][symbol] = {
            "status": value, "detail": detail, "source": source,
        }
        if value != FetchStatus.READY.value:
            group = {"MISSING": "missing_cache", "STALE": "stale_history",
                     "INVALID": "invalid_data"}.get(value, failure_category(detail))
            self._active_scan["failure_groups"][group].append(symbol)

    def record_scanner_outcome(self, symbol, side=None, eligible=True, reason=""):
        if self._active_scan is None:
            return
        self._active_scan["scanner"][symbol] = {
            "eligible": bool(eligible),
            "side": getattr(side, "value", side) if eligible else None,
            "reason": reason if not eligible else "",
        }

    def finish_scan_cycle(self):
        if self._active_scan is None:
            return self._last_scan
        active = self._active_scan
        historical = Counter(item["status"] for item in active["historical"].values())
        scanner_rows = active["scanner"].values()
        sides = Counter(item["side"] or "NONE" for item in scanner_rows if item["eligible"])
        skipped_reasons = Counter()
        for symbol in active["requested"]:
            outcome = active["scanner"].get(symbol, {})
            if outcome.get("eligible"):
                continue
            history = active["historical"].get(symbol, {})
            reason = outcome.get("reason")
            if not reason:
                if symbol in active["failure_groups"].get("insufficient_history", ()):
                    reason = "insufficient_history"
                else:
                    reason = {"MISSING": "historical_missing", "INVALID": "historical_invalid"}.get(
                        history.get("status"), "other"
                    )
            skipped_reasons[reason] += 1
        finished = self._now()
        self._last_scan = {
            "started_at": active["started_at"].isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": max(0, (finished - active["started_at"]).total_seconds()),
            "requested": len(active["requested"]),
            "historical": {
                **self._blank_counts(("READY", "MISSING", "STALE", "INVALID", "ERROR")),
                **dict(historical),
            },
            "scanner": {
                "ELIGIBLE": sum(item["eligible"] for item in active["scanner"].values()),
                "SKIPPED": len(active["requested"]) - sum(item["eligible"] for item in active["scanner"].values()),
                "SKIPPED_REASONS": dict(skipped_reasons),
                **self._blank_counts(("BUY", "SELL", "NONE")), **dict(sides),
            },
            "failure_groups": {
                group: list(symbols) for group, symbols in active["failure_groups"].items()
            },
        }
        self._active_scan = None
        return self._last_scan

    def _validate_history(self, symbol, candles, *, allow_no_recent_trade=False):
        closed = [item for item in candles if item.is_closed]
        validate_candles(symbol, closed, self.required_bars)
        validate_market_times(closed, self.calendar, self.exchanges[symbol])
        now = self._now()
        if any(item.timestamp > now for item in closed):
            raise InvalidData("Historical cache chứa nến đóng trong tương lai.")
        expected = self.calendar.latest_end(now, self.exchanges[symbol], "1d")
        if closed[-1].timestamp > expected:
            raise InvalidData("Historical daily chứa nến sau phiên đóng gần nhất.")
        if closed[-1].timestamp < expected and not allow_no_recent_trade:
            raise StaleData(
                f"Historical daily dừng ở {closed[-1].session}; cần kiểm tra phiên đóng {expected.date()}."
            )
        return closed

    def _read_provider_history(self, provider, symbol):
        source = getattr(provider, "source", "UNKNOWN")
        result_fetch = getattr(provider, "get_historical_result", None)
        if result_fetch is not None:
            result = result_fetch(symbol, "1d")
            if not isinstance(result, ProviderResult):
                raise InvalidData("Provider historical sai data contract.")
            if result.value is None or result.meta.status is not FetchStatus.READY:
                detail = result.error or result.meta.fallback_reason or result.meta.status.value
                if result.meta.status is FetchStatus.MISSING:
                    raise DataNotReady(detail)
                if result.meta.status is FetchStatus.INVALID:
                    raise InvalidData(detail)
                raise DataUnavailable(detail)
            return result.value, result.meta.fetched_at, result.meta.source or source
        cached = getattr(provider, "get_cached_candles", None)
        if cached is not None:
            candles, fetched = cached(symbol, "1d")
            return candles, fetched, source
        fetch = getattr(provider, "get_historical_candles", None)
        if fetch is not None:
            return fetch(symbol, "1d"), None, source
        batch_fetch = getattr(provider, "get_candles_batch", None)
        if batch_fetch is None:
            raise DataUnavailable("Provider không có historical cache API.")
        batch = batch_fetch((symbol,), ("1d",))
        if not isinstance(batch, CandleBatch):
            raise InvalidData("Provider batch sai data contract.")
        pair = (symbol, "1d")
        meta = batch.meta.get(pair, FetchMeta(source, None, FetchStatus.ERROR))
        detail = batch.errors.get(pair) or meta.fallback_reason or meta.status.value
        if pair not in batch.candles or meta.status is not FetchStatus.READY:
            if meta.status is FetchStatus.STALE:
                raise StaleData(detail)
            if meta.status is FetchStatus.MISSING:
                raise DataNotReady(detail)
            if meta.status is FetchStatus.INVALID:
                raise InvalidData(detail)
            raise DataUnavailable(detail)
        return batch.candles[pair], meta.fetched_at, meta.source or source

    def _historical(self, symbol):
        failures = []
        if self.daily_cache is not None:
            try:
                candles, fetched, source = self.daily_cache.read(symbol)
                expected = self.calendar.latest_end(self._now(), self.exchanges[symbol], "1d")
                if fetched < expected:
                    raise StaleData(
                        f"Historical cache chưa được refresh sau phiên đóng {expected.date()}."
                    )
                return self._validate_history(
                    symbol, candles, allow_no_recent_trade=True
                ), fetched, source
            except Exception as error:
                failures.append(error)
        providers = (self.primary, self.backup) if self.enable_failover else (self.primary,)
        for provider in providers:
            try:
                candles, fetched, source = self._read_provider_history(provider, symbol)
                expected = self.calendar.latest_end(self._now(), self.exchanges[symbol], "1d")
                if fetched is not None and fetched < expected:
                    raise StaleData(
                        f"Nguồn historical chưa được refresh sau phiên đóng {expected.date()}."
                    )
                candles = self._validate_history(
                    symbol, candles, allow_no_recent_trade=(provider is self.backup)
                )
                if self.daily_cache is not None:
                    self.daily_cache.write(symbol, candles, source, fetched or self._now())
                return candles, fetched, source
            except Exception as error:
                failures.append(error)
        if not failures:
            raise DataNotReady("Chưa có historical daily cache.")
        precedence = {FetchStatus.INVALID: 4, FetchStatus.STALE: 3,
                      FetchStatus.ERROR: 2, FetchStatus.MISSING: 1}
        raise max(failures, key=lambda item: precedence[_error_status(item)])

    def get_candles_batch(self, symbols, timeframes):
        if tuple(timeframes) != ("1d",):
            raise ValueError("Data pipeline chỉ hỗ trợ historical khung 1d.")
        result = CandleBatch()
        for symbol in tuple(symbols):
            pair = (symbol, "1d")
            try:
                history, fetched, source = self._historical(symbol)
                expected = self.calendar.latest_end(self._now(), self.exchanges[symbol], "1d")
                no_recent_trade = bool(history and history[-1].timestamp < expected)
                meta = FetchMeta(
                    source, fetched, FetchStatus.READY,
                    fallback_used=source != getattr(self.primary, "source", ""),
                    fallback_reason="no_recent_trade" if no_recent_trade else "",
                )
                result.candles[pair], result.meta[pair] = history, meta
                self._last_meta[pair] = meta
                self._record_history(symbol, FetchStatus.READY, source=source)
            except Exception as error:
                status = _error_status(error)
                detail = str(error)
                meta = FetchMeta("", None, status, status is FetchStatus.STALE,
                                 fallback_reason=detail)
                result.errors[pair], result.meta[pair] = detail, meta
                self._last_meta[pair] = meta
                self._record_history(symbol, status, detail)
        return result

    def get_candles(self, symbol, timeframe="1d"):
        batch = self.get_candles_batch((symbol,), (timeframe,))
        pair = (symbol, timeframe)
        meta = batch.meta.get(pair)
        if meta is None or meta.status is not FetchStatus.READY:
            raise DataUnavailable(batch.errors.get(pair) or "Không có historical daily hợp lệ.")
        return batch.candles[pair]

    def get_historical_candles(self, symbol, timeframe="1d"):
        if timeframe != "1d":
            raise ValueError("Historical cache chỉ hỗ trợ 1d.")
        return self._historical(symbol)[0]

    @staticmethod
    def _financial_result(provider, symbol):
        fetch = getattr(provider, "get_financials_result", None)
        if fetch is not None:
            result = fetch(symbol)
            if not isinstance(result, ProviderResult):
                raise InvalidData("Provider financial sai data contract.")
            return result
        value = provider.get_financials(symbol)
        return ProviderResult(
            value,
            FetchMeta(getattr(provider, "source", "UNKNOWN"), None,
                      FetchStatus.READY if value is not None else FetchStatus.ERROR),
        )

    def get_financials_result(self, symbol):
        primary = self._financial_result(self.primary, symbol)
        if primary.value is not None and primary.meta.status is FetchStatus.READY:
            return primary
        if not self.enable_failover:
            return primary
        backup = self._financial_result(self.backup, symbol)
        if backup.value is not None and backup.meta.status is FetchStatus.READY:
            return ProviderResult(
                backup.value,
                replace(backup.meta, fallback_used=True,
                        fallback_reason=failure_category(primary.error or primary.meta.status.value)),
            )
        status = backup.meta.status if backup.meta.status is not FetchStatus.READY else FetchStatus.ERROR
        return ProviderResult(
            None,
            FetchMeta("", backup.meta.fetched_at or primary.meta.fetched_at, status,
                      fallback_used=True,
                      fallback_reason=(f"primary={failure_category(primary.error)};"
                                       f"backup={failure_category(backup.error)}")),
            backup.error or primary.error,
        )

    def get_financials(self, symbol):
        result = self.get_financials_result(symbol)
        if result.value is None or result.meta.status is not FetchStatus.READY:
            raise DataUnavailable(result.error or result.meta.fallback_reason
                                  or "Tài chính chưa sẵn sàng.")
        return result.value

    def status_hint(self, symbol, timeframe=None):
        item = self._last_meta.get((symbol, timeframe or "1d"))
        if item is None:
            return ""
        if item.status is FetchStatus.STALE:
            return "Lịch sử ngày chưa có phiên đóng mới nhất; không tính tín hiệu mới."
        if item.fallback_used:
            return "Chuỗi lịch sử hoàn chỉnh đang dùng nguồn dự phòng."
        return ""

    def status_snapshot(self):
        return {
            "historical_primary": getattr(self.primary, "source", "UNKNOWN"),
            "historical_fallback": getattr(self.backup, "source", "UNKNOWN"),
            "failover": self.enable_failover,
            "scan": self._last_scan,
        }
