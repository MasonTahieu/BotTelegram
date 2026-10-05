"""Optional per-user fundamental filters. Missing/old data never passes an enabled rule."""

import math

RULES = {"roe_min": ("roe", "min", 100), "pe_max": ("pe", "max", 1),
         "pb_max": ("pb", "max", 1), "debt_max": ("debt_to_equity", "max", 1),
         "eps_min": ("eps", "min", 1), "illiq_max": (None, "max", 1)}


def validate_rule(name, value):
    if name not in RULES or not math.isfinite(value) or value < 0 or value > 1_000_000:
        raise ValueError("Dùng roe_min, pe_max, pb_max, debt_max, eps_min hoặc illiq_max với số không âm, tối đa 1000000.")
    return value


def calculate_amihud(candles, window=20):
    """Mean absolute log return per traded VND over closed daily sessions."""
    if window < 1:
        raise ValueError("Amihud window phải dương.")
    recent = [candle for candle in candles if candle.is_closed and candle.timeframe == "1d"][-(window + 1):]
    if len(recent) < window + 1:
        return None
    total = 0.0
    for previous, current in zip(recent, recent[1:]):
        if (current.session <= previous.session or
                not math.isfinite(previous.close) or previous.close <= 0 or
                not math.isfinite(current.close) or current.close <= 0 or current.volume <= 0):
            return None
        traded_value = current.close * current.volume
        if not math.isfinite(traded_value) or traded_value <= 0:
            return None
        total += abs(math.log(current.close) - math.log(previous.close)) / traded_value
    result = total / window
    return result if math.isfinite(result) else None


class FinancialFilter:
    def __init__(self, repository, provider, clock):
        self.repository, self.provider, self.clock = repository, provider, clock

    def illiq(self, symbol):
        try:
            load = getattr(self.provider, "get_historical_candles", None)
            candles = load(symbol, "1d") if load else self.provider.get_candles(symbol, "1d")
            now = self.clock.now()
            closed = [candle for candle in candles if candle.symbol == symbol
                      and candle.timestamp <= now
                      and (candle.observed_at is None or candle.observed_at <= now)]
            return calculate_amihud(closed)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def check(self, chat_id, symbol):
        rules = self.repository.financial_filter(chat_id)
        if not rules:
            return True, "Chưa bật bộ lọc tài chính."
        item = None
        financial_rules = [name for name in rules if name != "illiq_max"]
        if financial_rules:
            try:
                item = self.provider.get_financials(symbol)
                today = self.clock.now().date()
                if item is None or item.symbol != symbol:
                    return False, "Thiếu báo cáo tài chính."
                if not 0 <= (today - item.published_on).days <= 400:
                    return False, "Báo cáo chưa công bố hoặc đã quá 400 ngày."
                if item.observed_on is not None and not 0 <= (today - item.observed_on).days <= 100:
                    return False, "Bản tải tài chính chưa hợp lệ hoặc đã quá 100 ngày."
                self.repository.save_financials(self.provider.source, item)
                for name in financial_rules:
                    threshold = rules[name]
                    field, direction, scale = RULES[name]
                    value = getattr(item, field)
                    if value is None or not math.isfinite(value):
                        return False, f"Thiếu chỉ tiêu {field}."
                    if field in {"pe", "pb"} and value <= 0:
                        return False, f"{field.upper()} không dương."
                    actual = value * scale
                    if direction == "min" and actual < threshold or direction == "max" and actual > threshold:
                        return False, f"Không đạt {name}={threshold:g} (thực tế {actual:g})."
            except (OSError, ValueError, KeyError, TypeError):
                return False, "Chưa có dữ liệu tài chính hợp lệ."
        if "illiq_max" in rules:
            value = self.illiq(symbol)
            if value is None:
                return False, "Thiếu 21 nến ngày đã đóng hoặc giá trị giao dịch hợp lệ để tính ILLIQ."
            if value > rules["illiq_max"]:
                return False, f"Không đạt illiq_max={rules['illiq_max']:g} (thực tế {value:g})."
        if item is not None:
            return True, f"Đạt bộ lọc; kỳ {item.period}, công bố {item.published_on:%d/%m/%Y}."
        return True, "Đạt bộ lọc thanh khoản."
