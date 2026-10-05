from fintech_bot.config import StrategySettings
from fintech_bot.data.validation import validate_candles
from fintech_bot.domain import Candle, Signal, SignalSide
from fintech_bot.strategies.indicators import ema, rsi


class EmaRsiStrategy:
    def __init__(self, settings: StrategySettings) -> None:
        self.settings = settings
        self.required_bars = settings.required_bars

    def evaluate(
        self, symbol: str, candles: list[Candle], *, source: str, is_demo: bool,
    ) -> Signal:
        validate_candles(symbol, candles, self.required_bars)
        settings = self.settings
        closes = [candle.close for candle in candles]
        fast = ema(closes, settings.ema_fast)
        slow = ema(closes, settings.ema_slow)
        strength = rsi(closes, settings.rsi_period)
        # The minimum history guarantees that these last two values exist.
        previous_fast, current_fast = float(fast[-2]), float(fast[-1])
        previous_slow, current_slow = float(slow[-2]), float(slow[-1])
        previous_rsi, current_rsi = float(strength[-2]), float(strength[-1])
        cross_up = previous_fast <= previous_slow and current_fast > current_slow
        cross_down = previous_fast >= previous_slow and current_fast < current_slow
        rsi_exit = previous_rsi > settings.sell_rsi_level and current_rsi <= settings.sell_rsi_level
        side = SignalSide.NONE
        reason = "Chưa có giao cắt hoặc điều kiện Mua/Bán mới tại phiên cuối."
        # SELL has priority if exit and entry conditions occur on the same candle.
        if cross_down or rsi_exit:
            side = SignalSide.SELL
            reasons = []
            if cross_down:
                reasons.append(f"EMA{settings.ema_fast} cắt xuống EMA{settings.ema_slow}")
            if rsi_exit:
                reasons.append(f"RSI cắt xuống ngưỡng {settings.sell_rsi_level:g} từ vùng quá mua")
            reason = "; ".join(reasons) + "."
        elif cross_up and current_rsi > settings.buy_rsi_min:
            side = SignalSide.BUY
            reason = (
                f"EMA{settings.ema_fast} cắt lên EMA{settings.ema_slow} "
                f"và RSI > {settings.buy_rsi_min:g}."
            )
        return Signal(
            symbol=symbol, session=candles[-1].session, side=side,
            strategy_id=settings.strategy_id, reason=reason,
            indicators={"close": closes[-1], "ema_fast": current_fast,
                        "ema_slow": current_slow, "rsi": current_rsi},
            source=source, is_demo=is_demo,
            closed_at=candles[-1].closed_at, timeframe=candles[-1].timeframe,
            confirmed=candles[-1].is_closed,
            observed_at=candles[-1].observed_at,
        )
