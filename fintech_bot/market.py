"""Local clock and configurable Vietnamese cash-equity calendar."""

from datetime import date, datetime, time, timedelta
from functools import lru_cache

from fintech_bot.domain import VIETNAM


class Clock:
    def now(self) -> datetime:
        return datetime.now(VIETNAM)


class ReplayClock(Clock):
    def __init__(self, current: datetime) -> None:
        self.current = current.astimezone(VIETNAM)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current += timedelta(seconds=seconds)
        return self.current


class MarketCalendar:
    """Daily market close times; exceptional holidays are supplied locally."""

    def __init__(self, holidays: tuple[date, ...] = ()) -> None:
        self.holidays = frozenset(holidays)
        # Per-calendar bounded cache; all symbols on the same exchange share slots.
        self._cached_ends = lru_cache(maxsize=4096)(self._build_ends)
        self._cached_slots = lru_cache(maxsize=4096)(self._build_slots)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_scan_time(self, now: datetime) -> bool:
        local = now.astimezone(VIETNAM)
        t = local.time()
        # One minute of grace consumes the final bar after each session closes.
        return self.is_trading_day(local.date()) and (
            time(9) <= t <= time(11, 31) or time(13) <= t <= time(15, 1)
        )

    def ends(self, day: date, exchange: str, timeframe: str) -> list[datetime]:
        return list(self._cached_ends(day, exchange, timeframe))

    def slots(self, day: date, exchange: str, timeframe: str) -> frozenset[datetime]:
        return self._cached_slots(day, exchange, timeframe)

    def _build_slots(self, day, exchange, timeframe):
        return frozenset(self._cached_ends(day, exchange, timeframe))

    def _build_ends(self, day: date, exchange: str, timeframe: str) -> tuple[datetime, ...]:
        if exchange not in {"HOSE", "HNX", "UPCOM"} or timeframe != "1d":
            raise ValueError("Sàn hoặc khung nến không hợp lệ.")
        if not self.is_trading_day(day):
            return ()
        return (datetime.combine(day, time(14, 45) if exchange == "HOSE" else time(15), VIETNAM),)

    def latest_end(self, now: datetime, exchange: str, timeframe: str) -> datetime:
        return self.history_ends(now, exchange, timeframe, 1)[-1]

    def history_ends(self, now: datetime, exchange: str, timeframe: str, count: int) -> list[datetime]:
        now = now.astimezone(VIETNAM)
        day = now.date()
        found = []
        for _ in range(max(370, count * 4)):
            found = [end for end in self.ends(day, exchange, timeframe) if end <= now] + found
            if len(found) >= count:
                return found[-count:]
            day -= timedelta(days=1)
        raise ValueError("Không tìm đủ phiên trong lịch cấu hình.")

    def next_end(self, after: datetime, exchange: str, timeframe: str) -> datetime:
        day = after.astimezone(VIETNAM).date()
        for _ in range(370):
            for end in self.ends(day, exchange, timeframe):
                if end > after:
                    return end
            day += timedelta(days=1)
        raise ValueError("Không tìm thấy phiên kế tiếp.")
