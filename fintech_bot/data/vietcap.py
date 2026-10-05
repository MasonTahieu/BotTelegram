"""Normalize saved Vietcap responses; scanning never waits for HTTP downloads."""

import math
from datetime import date, datetime
from pathlib import Path

from fintech_bot.data.base import CandleBatch, FetchMeta, FetchStatus, ProviderResult
from fintech_bot.data.readiness import DataNotReady, DataUnavailable, InvalidData, StaleData
from fintech_bot.data.validation import validate_candles, validate_market_times
from fintech_bot.data.vietcap_http import check_symbol, read_json
from fintech_bot.domain import Candle, FinancialSnapshot, Instrument, VIETNAM


def fetched_at(envelope):
    value = datetime.fromisoformat(envelope["fetched_at"])
    if value.utcoffset() is None:
        raise ValueError("Thời gian tải dữ liệu thiếu múi giờ.")
    return value.astimezone(VIETNAM)


def parse_universe(envelope):
    rows = envelope["data"]
    if not isinstance(rows, list):
        raise ValueError("Danh sách mã Vietcap sai định dạng.")
    instruments = {}
    exchanges = {"HSX": "HOSE", "HOSE": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"}
    for row in rows:
        if row.get("type") != "STOCK" or row.get("board") not in exchanges:
            continue
        symbol = check_symbol(row["symbol"])
        item = Instrument(symbol, exchanges[row["board"]], row.get("organName") or "")
        if symbol in instruments and instruments[symbol] != item:
            raise ValueError(f"Danh sách mã có thông tin mâu thuẫn: {symbol}.")
        instruments[symbol] = item
    if not instruments:
        raise ValueError("Danh sách cổ phiếu Vietcap rỗng.")
    # 'active' means selected for attempted scanning, not verified trading eligibility.
    return sorted(instruments.values(), key=lambda item: (item.exchange, item.symbol))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Nguồn trả giá trị không phải số hữu hạn.")
    return float(value)


def _failure_category(entry):
    category = entry.get("category")
    if category:
        return category
    text = str(entry.get("error", "")).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "429" in text:
        return "rate_limit"
    if "chưa xác nhận mã được giao dịch" in text:
        return "source_unconfirmed"
    if "schema" in text or "json" in text or "định dạng" in text:
        return "invalid_schema"
    return "provider_exception"


def _raw_prices(envelope, symbol):
    data = envelope["data"]
    if not isinstance(data, list) or len(data) != 1 or data[0].get("symbol") != symbol:
        raise ValueError("Nguồn trả rỗng hoặc sai mã cổ phiếu.")
    row = data[0]
    columns = [row.get(field) for field in ("t", "o", "h", "l", "c", "v")]
    if any(not isinstance(column, list) for column in columns) or len({len(x) for x in columns}) != 1:
        raise ValueError("Các cột giá và thời gian không cùng độ dài.")
    result = []
    previous = None
    for timestamp, o, h, l, c, v in zip(*columns):
        stamp = datetime.fromtimestamp(int(timestamp), VIETNAM)
        if previous is not None and stamp <= previous:
            raise ValueError("Mốc thời gian bị trùng hoặc không tăng dần.")
        prices = [_number(x) for x in (o, h, l, c)]
        volume = _number(v)
        if min(prices) <= 0 or prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
            raise ValueError("OHLC không hợp lệ.")
        if volume < 0 or not volume.is_integer():
            raise ValueError("Khối lượng không phải số nguyên không âm.")
        result.append((stamp, *prices, int(volume)))
        previous = stamp
    return result


def parse_candles(envelope, symbol, exchange, timeframe, calendar):
    if timeframe != "1d":
        raise ValueError("Vietcap runtime chỉ hỗ trợ nến ngày 1d.")
    observed = fetched_at(envelope)
    raw = _raw_prices(envelope, symbol)
    result = []
    for stamp, o, h, l, c, volume in raw:
        ends = calendar.ends(stamp.date(), exchange, "1d")
        if not ends:
            raise ValueError(f"Nguồn có nến ngày ngoài lịch giao dịch: {stamp.date()}.")
        end = ends[0]
        if stamp.date() > observed.date():
            raise ValueError("Nguồn trả ngày giao dịch trong tương lai.")
        closed = end <= observed
        result.append(Candle(symbol, stamp.date(), o, h, l, c, volume, end, "1d", closed,
                             None if closed else observed))
    validate_candles(symbol, result, 1)
    validate_market_times(result, calendar, exchange)
    return result


def parse_financials(ratios_envelope, income_envelope, symbol):
    ratios = ratios_envelope["data"].get("data")
    income = income_envelope["data"].get("data")
    if not isinstance(ratios, list) or not isinstance(income, dict):
        raise ValueError("Nguồn tài chính thiếu bảng chỉ tiêu/báo cáo.")
    observed = max(fetched_at(ratios_envelope), fetched_at(income_envelope))
    reports = {}
    organ_codes = set()
    for section in ("years", "quarters"):
        for row in income.get(section, []):
            if row.get("ticker") != symbol:
                raise ValueError("Báo cáo tài chính trả sai mã.")
            organ_codes.add(row.get("organCode"))
            reports[int(row["yearReport"]), int(row["lengthReport"])] = row
    result = []
    for row in ratios:
        if row.get("organCode") not in organ_codes:
            raise ValueError("Chỉ số tài chính trả sai mã.")
        key = int(row["yearReport"]), int(row["quarter"])
        report = reports.get(key)
        if report is None or not report.get("publicDate"):
            continue  # Unknown publication dates must not be invented.
        published = date.fromisoformat(report["publicDate"][:10])
        if published > observed.date() or key[1] not in {1, 2, 3, 4, 5}:
            continue
        def optional(value):
            return None if value is None else _number(value)
        period = str(key[0]) if key[1] == 5 else f"{key[0]}-Q{key[1]}"
        result.append(FinancialSnapshot(symbol, period, published,
            eps=optional(report.get("isa23")), pe=optional(row.get("pe")), pb=optional(row.get("pb")),
            roe=optional(row.get("roe")), debt_to_equity=optional(row.get("debtToEquity")),
            observed_on=observed.date(), ratio_basis=row.get("ratioType", "unknown")))
    if not result:
        raise ValueError("Không có kỳ tài chính khớp báo cáo và ngày công bố.")
    return sorted(result, key=lambda x: (x.period[:4], 5 if len(x.period) == 4 else int(x.period[-1])))


class VietcapCacheProvider:
    is_demo = False
    display_name = "Vietcap Direct"

    def __init__(self, directory: Path, calendar):
        self.directory, self.calendar = Path(directory), calendar
        self.source = "VIETCAP_DIRECT"
        if not (self.directory / "universe.json").exists():
            raise ValueError("Chưa có danh sách Vietcap. Chạy prepare-vietcap.cmd trước lần sử dụng đầu.")
        self.instruments = parse_universe(read_json(self.directory / "universe.json"))
        self.exchanges = {item.symbol: item.exchange for item in self.instruments}
        self._cache = {}

    def _read(self, name):
        path = self.directory / (name + ".json")
        failure_path = self.directory / "failures.json"
        if failure_path.exists():
            try:
                failure_stamp = failure_path.stat().st_mtime_ns
                previous = self._cache.get("failures")
                if previous is None or previous[0] != failure_stamp:
                    previous = (failure_stamp, read_json(failure_path))
                    self._cache["failures"] = previous
                failure = previous[1].get(name)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise InvalidData("Sổ theo dõi lỗi dữ liệu bị hỏng.") from error
            if failure:
                # A newer atomically replaced cache supersedes a stale failure tombstone.
                failed_at = failure.get("at")
                try:
                    failed_stamp = datetime.fromisoformat(failed_at).timestamp() if failed_at else float("inf")
                except (TypeError, ValueError):
                    failed_stamp = float("inf")
                if not path.exists() or path.stat().st_mtime <= failed_stamp:
                    category = _failure_category(failure)
                    detail = str(failure.get("error", ""))
                    if ("\\" in detail or "/" in detail or "file specified" in detail.lower()
                            or len(detail) > 300):
                        detail = category
                    raise DataUnavailable(f"Lần cập nhật mới nhất thất bại: {detail or category}.")
        if not path.exists():
            raise DataNotReady(f"Chưa có cache {name.rsplit('-', 1)[-1]} cho mã yêu cầu.")
        try:
            stamp = path.stat().st_mtime_ns
        except OSError as error:
            raise DataNotReady("Cache dữ liệu chưa sẵn sàng.") from error
        cached = self._cache.get(name)
        if cached is None or cached[0] != stamp:
            try:
                cached = (stamp, read_json(path))
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise InvalidData("Cache dữ liệu bị rỗng, hỏng hoặc không phải JSON hợp lệ.") from error
            self._cache[name] = cached
        return cached

    def get_candles(self, symbol, timeframe="1d"):
        return self.get_historical_candles(symbol, timeframe)

    def status_hint(self, symbol):
        if not (self.directory / f"{symbol}-quote.json").exists():
            return ""
        from fintech_bot.data.vietcap_quotes import quote_candle, NoTradesYet, ACTIVE_STATES
        warning = ""
        try:
            _, quote = self._read(symbol + "-quote")
            warning = ACTIVE_STATES.get(quote["data"]["listingInfo"].get("tradingStatus"), "")
            quote_candle(quote, symbol, self.exchanges[symbol], self.calendar)
        except NoTradesYet as waiting:
            return " ".join(part for part in (str(waiting), warning) if part)
        except (OSError, ValueError, KeyError, TypeError):
            return "Bảng giá mới nhất chưa hợp lệ."
        return warning

    def get_historical_candles(self, symbol, timeframe="1d"):
        check_symbol(symbol)
        if timeframe != "1d":
            raise ValueError("Bot chỉ hỗ trợ khung nến 1d.")
        stamp, envelope = self._read(symbol + "-daily")
        key = (symbol, timeframe)
        cached = self._cache.get(key)
        if cached is None or cached[0] != stamp:
            try:
                candles = parse_candles(envelope, symbol, self.exchanges[symbol], timeframe, self.calendar)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise InvalidData("Cache nến không vượt qua kiểm tra dữ liệu.") from error
            cached = (stamp, candles)
            self._cache[key] = cached
        return list(cached[1])

    def get_historical_result(self, symbol, timeframe="1d"):
        try:
            candles = self.get_historical_candles(symbol, timeframe)
            _, envelope = self._read(symbol + "-daily")
            return ProviderResult(candles, FetchMeta(self.source, fetched_at(envelope), FetchStatus.READY))
        except DataNotReady as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.MISSING), str(error))
        except InvalidData as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.INVALID), str(error))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.ERROR), str(error))

    def get_quote_result(self, symbol, stale_after_seconds, now=None):
        from fintech_bot.data.vietcap_quotes import quote_candle, NoTradesYet
        now = now or datetime.now(VIETNAM)
        try:
            _, envelope = self._read(symbol + "-quote")
            observed = fetched_at(envelope)
            if (now - observed).total_seconds() > stale_after_seconds:
                raise StaleData("Bảng giá Vietcap đã quá freshness cho phép.")
            try:
                candle = quote_candle(envelope, symbol, self.exchanges[symbol], self.calendar)
            except NoTradesYet as waiting:
                return ProviderResult(None, FetchMeta(self.source, observed, FetchStatus.NO_TRADE), str(waiting))
            return ProviderResult((candle, envelope), FetchMeta(self.source, observed, FetchStatus.READY))
        except DataNotReady as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.MISSING), str(error))
        except StaleData as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.STALE, True), str(error))
        except InvalidData as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.INVALID), str(error))
        except DataUnavailable as error:
            status = FetchStatus.INVALID if "xác nhận mã được giao dịch" in str(error) else FetchStatus.ERROR
            return ProviderResult(None, FetchMeta(self.source, None, status), str(error))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.INVALID), str(error))

    def get_candles_batch(self, symbols, timeframes):
        result = CandleBatch()
        for symbol in symbols:
            for frame in timeframes:
                try:
                    result.candles[symbol, frame] = self.get_historical_candles(symbol, frame)
                    _, envelope = self._read(symbol + "-daily")
                    observed = fetched_at(envelope)
                    result.meta[symbol, frame] = FetchMeta(self.source, observed, FetchStatus.READY)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    result.errors[symbol, frame] = f"Chưa có dữ liệu Vietcap hợp lệ: {error}"
                    result.meta[symbol, frame] = FetchMeta(self.source, None, FetchStatus.ERROR)
        return result

    def get_financial_history(self, symbol):
        check_symbol(symbol)
        ratio_stamp, ratios = self._read(symbol + "-ratios")
        income_stamp, income = self._read(symbol + "-income")
        key, stamp = (symbol, "financials"), (ratio_stamp, income_stamp)
        cached = self._cache.get(key)
        if cached is None or cached[0] != stamp:
            try:
                history = parse_financials(ratios, income, symbol)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise InvalidData("Cache tài chính không vượt qua kiểm tra dữ liệu.") from error
            cached = (stamp, history)
            self._cache[key] = cached
        return list(cached[1])

    def get_financials(self, symbol):
        history = self.get_financial_history(symbol)
        return max(history, key=lambda x: (x.period[:4], int(x.period[-1]) if "-Q" in x.period else 4.5))

    def get_financials_result(self, symbol):
        try:
            snapshot = self.get_financials(symbol)
            _, ratios = self._read(symbol + "-ratios")
            _, income = self._read(symbol + "-income")
            observed = max(fetched_at(ratios), fetched_at(income))
            return ProviderResult(snapshot, FetchMeta(self.source, observed, FetchStatus.READY))
        except DataNotReady as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.MISSING), str(error))
        except InvalidData as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.INVALID), str(error))
        except DataUnavailable as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.ERROR), str(error))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return ProviderResult(None, FetchMeta(self.source, None, FetchStatus.ERROR), str(error))
