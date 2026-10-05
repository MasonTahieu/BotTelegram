import math

from fintech_bot.domain import Candle, VIETNAM


class DataValidationError(ValueError):
    pass


def validate_candles(symbol: str, candles: list[Candle], minimum: int) -> None:
    if not candles or len(candles) < minimum:
        raise DataValidationError(f"Cần ít nhất {max(1, minimum)} nến, hiện có {len(candles)}.")
    previous_session = None
    timeframe = candles[0].timeframe
    for candle in candles:
        if candle.symbol != symbol:
            raise DataValidationError("Dữ liệu chứa sai mã cổ phiếu.")
        if type(candle.is_closed) is not bool:
            raise DataValidationError("Trạng thái is_closed phải là true hoặc false.")
        if candle.timestamp.utcoffset() is None:
            raise DataValidationError("Thời gian nến cần có múi giờ.")
        if candle.timeframe != timeframe or timeframe != "1d":
            raise DataValidationError("Không được trộn các khung nến trong một chuỗi.")
        if candle.timestamp.astimezone(VIETNAM).date() != candle.session:
            raise DataValidationError("Ngày và thời gian đóng nến không khớp.")
        if candle.observed_at is not None and candle.observed_at.utcoffset() is None:
            raise DataValidationError("Thời gian quan sát phải có múi giờ.")
        if not candle.is_closed:
            if candle.observed_at is None:
                raise DataValidationError("Nến chưa đóng phải có thời gian quan sát.")
            if candle.observed_at >= candle.timestamp:
                raise DataValidationError("Nến chưa đóng phải được quan sát trước thời gian đóng.")
            if candle.observed_at.astimezone(VIETNAM).date() != candle.session:
                raise DataValidationError("Nến chưa đóng phải được quan sát trong cùng ngày giao dịch.")
        if previous_session is not None and candle.timestamp <= previous_session:
            raise DataValidationError("Thời gian nến phải tăng dần và không trùng nhau.")
        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(not math.isfinite(price) or price <= 0 for price in prices):
            raise DataValidationError("Giá phải là số hữu hạn lớn hơn 0.")
        if candle.low > min(candle.open, candle.close) or candle.high < max(candle.open, candle.close):
            raise DataValidationError("Giá cao/thấp không bao quanh giá mở/đóng cửa.")
        if type(candle.volume) is not int or candle.volume < 0:
            raise DataValidationError("Khối lượng phải là số nguyên không âm.")
        previous_session = candle.timestamp


def validate_market_times(candles, calendar, exchange):
    """Use the configured calendar, not the computer's wall clock or a provider convention."""
    slots_by_day = {}
    for candle in candles:
        key = (candle.session, candle.timeframe)
        if key not in slots_by_day:
            slots_by_day[key] = calendar.slots(candle.session, exchange, candle.timeframe)
        if candle.timestamp not in slots_by_day[key]:
            raise DataValidationError(
                f"Mốc đóng nến {candle.timestamp.isoformat()} không thuộc lịch {exchange}/{candle.timeframe}."
            )
        if not candle.is_closed and candle.observed_at.astimezone(VIETNAM).hour < 9:
            raise DataValidationError("Nến ngày đang hình thành không được quan sát trước giờ mở cửa.")
