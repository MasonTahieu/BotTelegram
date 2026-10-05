"""Deterministic synthetic fixtures, never market prices or real company data."""

from datetime import date, timedelta

from fintech_bot.domain import Candle, FinancialSnapshot


class DemoDataProvider:
    source = "synthetic-demo-v1"
    is_demo = True

    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols

    def get_candles(self, symbol: str) -> list[Candle]:
        if symbol not in self.symbols:
            raise ValueError(f"Mã {symbol} chưa thuộc danh sách demo.")
        # Different endings exercise BUY, SELL and NONE paths in the default strategy.
        scenario = self.symbols.index(symbol) % 3
        if scenario == 0:
            closes = [100.0] * 60 + [99.0] * 20 + [130.0]
        elif scenario == 1:
            closes = [100.0] * 60 + [101.0] * 20 + [70.0]
        else:
            closes = [100.0] * 81
        session = date(2026, 1, 5)
        candles = []
        for index, close in enumerate(closes):
            while session.weekday() >= 5:
                session += timedelta(days=1)
            opening = closes[max(index - 1, 0)]
            candles.append(Candle(
                symbol=symbol, session=session, open=opening,
                high=max(opening, close) + 1, low=min(opening, close) - 1,
                close=close, volume=100_000 + index * 500,
            ))
            session += timedelta(days=1)
        return candles

    def get_financials(self, symbol: str) -> FinancialSnapshot | None:
        # Intentionally absent until a real financial source is selected.
        return None

