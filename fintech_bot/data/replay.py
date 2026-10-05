"""An advancing, reproducible offline market. All prices are synthetic."""

from datetime import timedelta
import math

from fintech_bot.domain import Candle, FinancialSnapshot
from fintech_bot.market import MarketCalendar, ReplayClock


class ReplayDataProvider:
    source = "synthetic-replay-v2"
    is_demo = True

    def __init__(self, instruments, clock: ReplayClock, calendar: MarketCalendar):
        self.instruments = {item.symbol: item for item in instruments}
        self.scenarios = {item.symbol: index for index, item in enumerate(instruments)}
        self.clock = clock
        self.calendar = calendar
        self.series = {}
        for index, instrument in enumerate(instruments):
            # 81 completed sessions preserve the original BUY/SELL/NONE demo
            # scenarios without inventing an in-progress preview candle.
            ends = calendar.history_ends(clock.now(), instrument.exchange, "1d", 81)
            candles = []
            previous = 100.0
            for number, end in enumerate(ends):
                close = self._price(index, number)
                candles.append(self._candle(instrument.symbol, end, previous, close, number))
                previous = close
            self.series[instrument.symbol] = candles

    @staticmethod
    def _price(scenario, index):
        scenario %= 3
        if index < 60 or scenario == 2:
            return 100.0
        if index < 80:
            return 99.0 if scenario == 0 else 101.0
        if index == 80:
            return 130.0 if scenario == 0 else 70.0
        return round(100 + 20 * math.cos((index - 80) / 5) * (1 if scenario == 0 else -1), 4)

    @staticmethod
    def _candle(symbol, end, opening, close, number):
        return Candle(symbol, end.date(), opening, max(opening, close) + 1,
                      min(opening, close) - 1, close, 100_000 + number * 500,
                      closed_at=end, timeframe="1d", observed_at=end)

    def get_candles(self, symbol, timeframe="1d"):
        if timeframe != "1d":
            raise ValueError("Bot chỉ hỗ trợ khung nến 1d.")
        instrument = self.instruments[symbol]
        candles = self.series[symbol]
        scenario = self.scenarios[symbol]
        now = self.clock.now()
        end = self.calendar.next_end(candles[-1].timestamp, instrument.exchange, timeframe)
        while end <= now:
            close = self._price(scenario, len(candles))
            candles.append(self._candle(symbol, end, candles[-1].close, close, len(candles)))
            end = self.calendar.next_end(end, instrument.exchange, timeframe)
        return list(candles)

    def get_financials(self, symbol):
        # Explicitly synthetic data exercises the financial storage path.
        return FinancialSnapshot(symbol, "2026-Q2-DEMO", self.clock.now().date() - timedelta(days=30),
                                 eps=1000, pe=10, pb=1.5, roe=0.15, debt_to_equity=0.5,
                                 revenue_growth=0.1, profit_growth=0.1)
