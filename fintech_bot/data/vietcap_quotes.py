"""Daily candles from bulk priceboard OHLCV. Never manufacture intraday bars."""

from datetime import date, datetime, timedelta

from fintech_bot.data.vietcap import _number, fetched_at
from fintech_bot.domain import Candle, VIETNAM

# Explicit known source states, not a suffix rule (UNACTIVATED also ends in
# ACTIVATED). A warning/control flag is not by itself evidence of suspension.
ACTIVE_STATES = {
    "TRADING_ACTIVATED": "",
    "TRADING_FINANCIAL_REPORT_ACTIVATED": "Nguồn gắn cảnh báo liên quan báo cáo tài chính.",
    "TRADING_OTHER_VIOLATIONS_ACTIVATED": "Nguồn gắn cảnh báo vi phạm khác.",
    "TRADING_CONTROL_ACTIVATED": "Nguồn gắn trạng thái kiểm soát.",
    "TRADING_EXCEPTIONAL_PRICE_LIMIT_ACTIVATED": "Nguồn gắn biên độ giá đặc biệt.",
    "TRADING_INFORMATION_DISCLOSURE_ACTIVATED": "Nguồn gắn cảnh báo công bố thông tin.",
    "TRADING_RESTRICTION_ACTIVATED": "Nguồn gắn hạn chế giao dịch; cần đối chiếu lịch riêng của mã.",
}


class NoTradesYet(ValueError):
    def __init__(self, session):
        self.session = session
        super().__init__(f"Phiên {session} chưa có giao dịch; chỉ xem được lịch sử đã đóng.")


def quote_candle(envelope, symbol, exchange, calendar):
    row = envelope["data"]
    info, match = row["listingInfo"], row["matchPrice"]
    if info.get("symbol") != symbol or match.get("symbol") != symbol:
        raise ValueError("Bảng giá trả sai mã.")
    if {"HSX": "HOSE", "HOSE": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"}.get(info.get("board")) != exchange:
        raise ValueError("Bảng giá trả sai sàn.")
    if info.get("stockType") != "STOCK" or info.get("isDelisted") not in (0, False):
        raise ValueError("Mã không phải cổ phiếu đang niêm yết/đăng ký giao dịch.")
    if info.get("tradingStatus") not in ACTIVE_STATES:
        raise ValueError("Nguồn chưa xác nhận mã được giao dịch.")
    day = date.fromisoformat(info["tradingDate"])
    fetched = fetched_at(envelope)
    ends = calendar.ends(day, exchange, "1d")
    if not ends or day > fetched.date():
        raise ValueError("Ngày bảng giá ngoài lịch giao dịch hoặc ở tương lai.")
    end = ends[0]
    def stamp(value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Thời gian bảng giá thiếu múi giờ.")
        return parsed.astimezone(VIETNAM)
    traded = stamp(match["time"])
    if traded.date() != day or traded > fetched + timedelta(seconds=5):
        raise ValueError("Ngày khớp lệnh không khớp bảng giá.")
    if match.get("accumulatedVolume") == 0 and all(match.get(key) in (None, 0) for key in
                                                  ("openPrice", "highest", "lowest", "matchPrice")):
        raise NoTradesYet(day)
    prices = [_number(match[key]) for key in ("openPrice", "highest", "lowest", "matchPrice")]
    volume = _number(match["accumulatedVolume"])
    o, h, l, c = prices
    if min(prices) <= 0 or l > min(o, c) or h < max(o, c) or volume <= 0 or not volume.is_integer():
        raise ValueError("Bảng giá thiếu OHLCV hợp lệ hoặc chưa có giao dịch.")
    # Session labels can remain LO_AFTERNOON/EXTEND_HOUR over a weekend. Use
    # dated source timestamps, not those labels or a fresh HTTP fetch time.
    quote_time = traded
    bid_ask = row.get("bidAsk", {})
    if bid_ask.get("symbol") == symbol and bid_ask.get("time"):
        candidate = stamp(bid_ask["time"])
        if candidate.date() == day and candidate <= fetched + timedelta(seconds=5):
            quote_time = max(quote_time, candidate)
    # A last trade may legitimately occur before the session end. Closure is a
    # property of the exchange session at fetch time, not of the last trade.
    closed = fetched >= end + timedelta(seconds=60)
    return Candle(symbol, day, o, h, l, c, int(volume), end, "1d", closed, quote_time)
