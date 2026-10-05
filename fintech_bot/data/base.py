from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from fintech_bot.domain import Candle, FinancialSnapshot


class MarketDataProvider(Protocol):
    """Return complete, sorted histories, including source corrections; never stale fallbacks on errors."""
    source: str
    is_demo: bool

    def get_candles(self, symbol: str, timeframe: str = "1d") -> list[Candle]: ...

    def get_financials(self, symbol: str) -> FinancialSnapshot | None: ...


class FetchStatus(StrEnum):
    READY = "READY"
    NO_TRADE = "NO_TRADE"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"
    ERROR = "ERROR"


@dataclass(frozen=True)
class FetchMeta:
    """Provenance and readiness for one normalized provider result."""

    source: str
    fetched_at: datetime | None
    status: FetchStatus = FetchStatus.READY
    stale: bool = False
    fallback_used: bool = False
    fallback_reason: str = ""


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    value: T | None
    meta: FetchMeta
    error: str = ""


@dataclass
class CandleBatch:
    """A bounded refresh result. Every requested pair must have history or an error.

    The adapter merges incremental API updates with its history before returning.
    Missing pairs fail closed; the scanner never falls back to more API calls.
    """

    candles: dict[tuple[str, str], list[Candle]] = field(default_factory=dict)
    # Keep errors as the second positional field for existing provider adapters.
    errors: dict[tuple[str, str], str] = field(default_factory=dict)
    meta: dict[tuple[str, str], FetchMeta] = field(default_factory=dict)


class BatchMarketDataProvider(MarketDataProvider, Protocol):
    def get_candles_batch(self, symbols: tuple[str, ...], timeframes: tuple[str, ...]) -> CandleBatch: ...
