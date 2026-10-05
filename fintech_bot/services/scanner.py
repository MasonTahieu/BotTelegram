import logging
from dataclasses import dataclass, field, replace
from datetime import datetime

from fintech_bot.data.base import CandleBatch, FetchMeta, FetchStatus
from fintech_bot.data.readiness import public_data_error
from fintech_bot.data.validation import DataValidationError, validate_candles, validate_market_times
from fintech_bot.domain import Signal

logger = logging.getLogger(__name__)


@dataclass
class ScanReport:
    signals: list[Signal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    scanned_at: datetime | None = None


class Scanner:
    def __init__(self, provider, strategy, repository, instruments, settings, clock, calendar):
        self.provider, self.strategy, self.repository = provider, strategy, repository
        self.instruments = {item.symbol: item for item in instruments}
        self.symbols = tuple(self.instruments)
        self.settings, self.clock, self.calendar = settings, clock, calendar
        self.latest_report = ScanReport()
        self.last_checked = {}
        self.persisted_candles = {}
        self.evaluated = {}

    def begin_cycle(self, symbols=None):
        selected = list(symbols) if symbols is not None else list(self.symbols)
        self.latest_report = ScanReport(scanned_at=self.clock.now())
        begin = getattr(self.provider, "begin_scan_cycle", None)
        if begin is not None:
            begin(selected)

    def finish_cycle(self):
        finish = getattr(self.provider, "finish_scan_cycle", None)
        return finish() if finish is not None else None

    def _batch(self, selected, timeframes):
        fetch = getattr(self.provider, "get_candles_batch", None)
        active = tuple(symbol for symbol in selected if self.instruments[symbol].status == "active")
        if fetch is None or not active:
            return None
        try:
            result = fetch(active, timeframes)
            if not isinstance(result, CandleBatch) or not isinstance(result.candles, dict) or not isinstance(result.errors, dict):
                raise DataValidationError("Nguồn trả sai định dạng dữ liệu theo lô.")
            return result
        except Exception as error:
            # No per-symbol fallback: it could multiply calls after an outage/429.
            pairs = {(symbol, frame): f"Không cập nhật được lô dữ liệu: {error}"
                     for symbol in active for frame in timeframes}
            return CandleBatch(errors=pairs, meta={pair: FetchMeta(
                getattr(self.provider, "source", ""), None, FetchStatus.ERROR) for pair in pairs})

    def run(self, symbols=None, timeframes=None) -> ScanReport:
        automatic_cycle = symbols is None
        if automatic_cycle:
            self.begin_cycle()
        now = self.clock.now()
        report = ScanReport(scanned_at=now)
        selected = list(symbols) if symbols is not None else list(self.symbols)
        frames = tuple(timeframes) if timeframes is not None else ("1d",)
        if not frames or set(frames) != {"1d"}:
            raise ValueError("Scanner chỉ hỗ trợ khung nến 1d.")
        frames = tuple(dict.fromkeys(frames))
        if any(symbol not in self.instruments for symbol in selected):
            raise ValueError("Mã cần quét không thuộc danh sách cấu hình.")
        selected = list(dict.fromkeys(selected))
        batch = self._batch(selected, frames)
        for symbol in selected:
            instrument = self.instruments[symbol]
            if instrument.status != "active":
                report.skipped[symbol] = "Mã đang tạm ngừng hoặc không còn giao dịch."
                record = getattr(self.provider, "record_scanner_outcome", None)
                if record is not None:
                    record(symbol, eligible=False)
                continue
            for timeframe in frames:
                key = f"{symbol}/{timeframe}"
                try:
                    if batch is None:
                        raw = self.provider.get_candles(symbol, timeframe)
                        meta = FetchMeta(self.provider.source, now, FetchStatus.READY)
                    else:
                        pair = (symbol, timeframe)
                        meta = batch.meta.get(pair, FetchMeta(
                            getattr(self.provider, "source", ""), None, FetchStatus.READY))
                        if meta.status is not FetchStatus.READY:
                            report.skipped[symbol] = f"Historical {meta.status.value}; không phát tín hiệu mới. Xem /status."
                            self.last_checked.pop((symbol, timeframe), None)
                            self.repository.invalidate_pending_for_pair(symbol, timeframe)
                            record = getattr(self.provider, "record_scanner_outcome", None)
                            if record is not None:
                                record(symbol, eligible=False)
                            continue
                        if pair in batch.errors:
                            raise DataValidationError(batch.errors[pair])
                        if pair not in batch.candles:
                            raise DataValidationError("Lô dữ liệu thiếu mã/khung yêu cầu; không dùng kết quả cũ.")
                        raw = batch.candles[pair]
                    actual_source = meta.source or getattr(self.provider, "source", "UNKNOWN")
                    now = self.clock.now()
                    if not raw:
                        raise DataValidationError("Nguồn không trả nến.")
                    if any(item.symbol != symbol or item.timeframe != timeframe for item in raw):
                        raise DataValidationError("Nguồn trả sai khung nến yêu cầu.")
                    # EMA/RSI is reproducible only from completed daily bars.
                    # Current-day quotes are displayed separately and never enter
                    # this series.
                    candles = [candle for candle in raw
                               if candle.is_closed and candle.timestamp <= now
                               and (candle.observed_at is None or candle.observed_at <= now)]
                    validate_candles(symbol, candles, self.strategy.required_bars)
                    validate_market_times(candles, self.calendar, instrument.exchange)
                    last = candles[-1]
                    expected = self.calendar.latest_end(now, instrument.exchange, timeframe)
                    if last.timestamp < expected and meta.fallback_reason == "no_recent_trade":
                        report.skipped[symbol] = (
                            f"Không có giao dịch ở phiên đóng gần nhất; nến gần nhất "
                            f"{last.session.isoformat()}. Không phát tín hiệu mới."
                        )
                        self.last_checked.pop((symbol, timeframe), None)
                        self.repository.invalidate_pending_for_pair(symbol, timeframe)
                        record = getattr(self.provider, "record_scanner_outcome", None)
                        if record is not None:
                            record(symbol, eligible=False, reason="no_recent_trade")
                        continue
                    if last.timestamp < expected:
                        raise DataValidationError(
                            f"Dữ liệu cũ; nến cuối {last.timestamp.isoformat()}, "
                            f"cần phiên đóng {expected.isoformat()}. Không phát tín hiệu mới."
                        )
                    if last.timestamp != expected:
                        raise DataValidationError("Nến cuối không khớp phiên giao dịch gần nhất.")
                    if last.volume == 0:
                        raise DataValidationError("Nến cuối chưa có khối lượng giao dịch; bỏ qua tín hiệu.")
                    cache_key = (actual_source, self.provider.is_demo, symbol, timeframe)
                    previous = self.persisted_candles.get(cache_key, {})
                    changed = [item for item in candles if previous.get(item.timestamp) != item]
                    if changed:
                        self.repository.save_candles(actual_source, changed)
                        self.persisted_candles[cache_key] = {item.timestamp: item for item in candles}
                    # Compare the whole immutable history: correcting an earlier close
                    # can change today's EMA even when the final bar is unchanged.
                    try:
                        source_hint = getattr(self.provider, "status_hint", lambda *_: "")(symbol, timeframe)
                    except TypeError:
                        source_hint = getattr(self.provider, "status_hint", lambda *_: "")(symbol)
                    inputs = (self.strategy, getattr(self.strategy, "settings", None), tuple(candles), instrument.exchange, source_hint)
                    cached = self.evaluated.get(cache_key)
                    if cached is not None and cached[0] == inputs:
                        signal = cached[1]
                    else:
                        signal = self.strategy.evaluate(symbol, candles, source=actual_source, is_demo=self.provider.is_demo)
                        signal = replace(signal, exchange=instrument.exchange)
                        if source_hint:
                            signal = replace(signal, reason=signal.reason + " " + source_hint)
                        self.repository.save_signal(signal)
                        self.evaluated[cache_key] = (inputs, signal)
                    report.signals.append(signal)
                    record = getattr(self.provider, "record_scanner_outcome", None)
                    if record is not None:
                        record(symbol, signal.side, eligible=True)
                    self.last_checked[symbol, timeframe] = self.clock.now()
                except Exception as error:
                    logger.error("Scan failed for %s: %s", key, error)
                    report.errors[key] = public_data_error(error)
                    self.last_checked.pop((symbol, timeframe), None)
                    self.repository.invalidate_pending_for_pair(symbol, timeframe)
                    record = getattr(self.provider, "record_scanner_outcome", None)
                    if record is not None:
                        record(symbol, eligible=False)
        report.scanned_at = self.clock.now()
        if symbols is None:
            self.latest_report = report
        else:
            previous = self.latest_report
            selected_pairs = {(symbol, frame) for symbol in selected for frame in frames}
            keep = lambda key: tuple(key.split("/", 1)) not in selected_pairs
            self.latest_report = ScanReport(
                [item for item in previous.signals if (item.symbol, item.timeframe) not in selected_pairs] + report.signals,
                {**{key: value for key, value in previous.errors.items() if keep(key)}, **report.errors},
                {**{key: value for key, value in previous.skipped.items()
                    if (key not in selected if "/" not in key else keep(key))}, **report.skipped},
                report.scanned_at,
            )
        if automatic_cycle:
            self.finish_cycle()
        return report

    def refresh_if_needed(self, symbol=None, timeframe=None):
        if symbol is not None:
            frames = (timeframe,) if timeframe else ("1d",)
            last_values = [self.last_checked.get((symbol, frame)) for frame in frames]
            if any(last is None or (self.clock.now() - last).total_seconds() > self.settings.cache_seconds
                   for last in last_values):
                return self.run([symbol], frames)
            return None
        last = self.latest_report.scanned_at
        if last is None or (self.clock.now() - last).total_seconds() > self.settings.cache_seconds:
            return self.run()
        return None

    def mark_unavailable(self, symbol, timeframe, message="Dữ liệu chưa sẵn sàng."):
        """Fail closed immediately when a requested refresh cannot become READY."""
        pair = (symbol, timeframe)
        key = f"{symbol}/{timeframe}"
        self.last_checked.pop(pair, None)
        self.repository.invalidate_pending_for_pair(symbol, timeframe)
        previous = self.latest_report
        self.latest_report = ScanReport(
            [item for item in previous.signals if (item.symbol, item.timeframe) != pair],
            {**{name: value for name, value in previous.errors.items() if name != key}, key: message},
            dict(previous.skipped),
            previous.scanned_at,
        )
