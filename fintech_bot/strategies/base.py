from typing import Protocol

from fintech_bot.domain import Candle, Signal


class Strategy(Protocol):
    required_bars: int

    def evaluate(
        self, symbol: str, candles: list[Candle], *, source: str, is_demo: bool,
    ) -> Signal: ...

