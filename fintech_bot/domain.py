"""Shared models. No data-source or Telegram dependencies belong here."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum


class SignalSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


VIETNAM = timezone(timedelta(hours=7), "Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str
    name: str = ""
    asset_type: str = "stock"
    status: str = "active"


@dataclass(frozen=True)
class Candle:
    symbol: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    closed_at: datetime | None = None
    timeframe: str = "1d"
    is_closed: bool = True
    observed_at: datetime | None = None

    @property
    def timestamp(self) -> datetime:
        return self.closed_at or datetime.combine(self.session, time(15), VIETNAM)


@dataclass(frozen=True)
class FinancialSnapshot:
    """Financial data; ratios use fractions (0.15 = 15%). Observed dates are not publication dates."""

    symbol: str
    period: str
    published_on: date
    eps: float | None = None
    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    debt_to_equity: float | None = None
    revenue_growth: float | None = None
    profit_growth: float | None = None
    observed_on: date | None = None
    ratio_basis: str = ""


@dataclass(frozen=True)
class Signal:
    symbol: str
    session: date
    side: SignalSide
    strategy_id: str
    reason: str
    indicators: dict[str, float]
    source: str
    is_demo: bool
    closed_at: datetime | None = None
    timeframe: str = "1d"
    confirmed: bool = True
    exchange: str = ""
    observed_at: datetime | None = None

    @property
    def key(self) -> str:
        """Stable logical event identity; source is provenance, not identity."""
        return "|".join((
            "demo" if self.is_demo else "live", self.strategy_id,
            self.symbol, self.timeframe, self.session.isoformat(),
            self.closed_at.isoformat() if self.closed_at else "",
            "confirmed" if self.confirmed else "preview", self.side.value,
        ))
