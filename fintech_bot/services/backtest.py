"""Small, explicit historical simulation; no calls to data or broker APIs."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path

from fintech_bot.data.validation import validate_candles
from fintech_bot.domain import SignalSide


@dataclass(frozen=True)
class BacktestSettings:
    initial_cash: float = 100_000_000
    fee_rate: float = 0.0015
    slippage_rate: float = 0.0005
    lot_size: int = 100
    min_holding_sessions: int = 2

    def __post_init__(self):
        import math
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("Vốn mô phỏng phải dương và hữu hạn.")
        if any(not math.isfinite(value) or not 0 <= value < 1 for value in (self.fee_rate, self.slippage_rate)):
            raise ValueError("Phí và trượt giá phải thuộc [0, 1).")
        if type(self.lot_size) is not int or self.lot_size < 1:
            raise ValueError("lot_size phải là số nguyên dương.")
        if type(self.min_holding_sessions) is not int or self.min_holding_sessions < 0:
            raise ValueError("Số phiên nắm giữ tối thiểu không được âm.")


def run_backtest(symbol, candles, strategy, *, source, is_demo, settings=None, start_date=None):
    settings = settings or BacktestSettings()
    candles = [item for item in candles if item.is_closed]
    validate_candles(symbol, candles, strategy.required_bars + 1)
    first_index = strategy.required_bars - 1
    if start_date is not None:
        eligible = [index for index, candle in enumerate(candles) if candle.session >= start_date]
        if not eligible or eligible[0] < first_index:
            raise ValueError("Khoảng backtest thiếu lịch sử khởi tạo hoặc không có nến.")
        first_index = eligible[0]
    if len(candles) - first_index < 2:
        raise ValueError("Cần ít nhất hai nến trong khoảng đánh giá.")
    days = {day: index for index, day in enumerate(sorted({item.session for item in candles}))}
    cash, quantity, entry_cost, entry_day = settings.initial_cash, 0, 0.0, -1
    pending = SignalSide.NONE
    pending_at = None
    trades, equity = [], []
    completed, wins, blocked_sells = 0, 0, 0
    for index in range(first_index, len(candles)):
        candle = candles[index]
        if pending == SignalSide.BUY and quantity == 0:
            price = candle.open * (1 + settings.slippage_rate)
            lot_cost = price * settings.lot_size * (1 + settings.fee_rate)
            quantity = int(cash // lot_cost) * settings.lot_size
            if quantity:
                entry_cost = quantity * price * (1 + settings.fee_rate)
                cash -= entry_cost
                entry_day = days[candle.session]
                trades.append({"side": "BUY", "signal_at": pending_at, "executed_at": candle.timestamp.isoformat(),
                               "price": price, "quantity": quantity, "cash_flow": -entry_cost})
        elif pending == SignalSide.SELL and quantity:
            if days[candle.session] - entry_day >= settings.min_holding_sessions:
                price = candle.open * (1 - settings.slippage_rate)
                proceeds = quantity * price * (1 - settings.fee_rate)
                cash += proceeds
                trades.append({"side": "SELL", "signal_at": pending_at, "executed_at": candle.timestamp.isoformat(),
                               "price": price, "quantity": quantity, "cash_flow": proceeds,
                               "pnl": proceeds - entry_cost})
                completed += 1
                wins += proceeds > entry_cost
                quantity = 0
            else:
                blocked_sells += 1
        pending = SignalSide.NONE
        signal = strategy.evaluate(symbol, candles[:index+1], source=source, is_demo=is_demo)
        pending, pending_at = signal.side, candle.timestamp.isoformat()
        equity.append(cash + quantity * candle.close)
    peak = settings.initial_cash
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak)
    return {
        "symbol": symbol, "source": source, "is_demo": is_demo, "timeframe": candles[0].timeframe,
        "first_evaluation": candles[first_index].timestamp.isoformat(),
        "last_bar": candles[-1].timestamp.isoformat(), "bars": len(candles),
        "assumptions": asdict(settings), "final_equity": equity[-1],
        "return_percent": (equity[-1] / settings.initial_cash - 1) * 100,
        "max_drawdown_percent": drawdown * 100, "closed_trades": completed,
        "win_rate_percent": wins / completed * 100 if completed else None,
        "open_quantity": quantity, "blocked_sells": blocked_sells, "trades": trades,
        "limitations": [
            "Dữ liệu giả lập, không đánh giá lợi nhuận thị trường thật." if is_demo else
            ("Dữ liệu historical Vietcap từ local cache; chưa xác minh đầy đủ điều chỉnh chia tách/cổ tức."
             if source == "VIETCAP_DIRECT" or source.startswith("vietcap-public:") else
             "Dữ liệu CSV do người dùng cung cấp, chưa đối chiếu nguồn."
             if source.startswith("local-csv:") else
             "Dữ liệu chưa xác minh đầy đủ điều chỉnh chia tách/cổ tức."),
            "Khớp ở giá mở nến kế tiếp; executed_at là mốc ĐÓNG của nến thực hiện, không phải giờ khớp thực tế.",
            "Giả định đủ thanh khoản, không khớp một phần; chưa mô phỏng biên độ giá, thuế bán hoặc corporate actions.",
            "Giữ tối thiểu số phiên cấu hình là mô hình đơn giản, không phải mô phỏng đầy đủ thanh toán T+2.",
            "Không ép bán cuối mẫu; định giá vị thế mở theo giá đóng cửa cuối, chưa trừ phí thoát vị thế.",
            "Tín hiệu ở nến cuối không được khớp nếu chưa có nến tiếp theo.",
        ],
    }


def write_backtest(result, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"backtest-{result['symbol']}-{result['timeframe']}"
    json_path = folder / (stem + ".json")
    md_path = folder / (stem + ".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    win = result['win_rate_percent']
    source = str(result.get("source", ""))
    source_note = ("**DEMO — KHÔNG PHẢI KẾT QUẢ ĐẦU TƯ THỰC**" if result["is_demo"] else
                   f"**Nguồn historical thực tế: {source}**" if source in {"VIETCAP_DIRECT", "VNSTOCK_KBS"} else
                   "**Nguồn dữ liệu: VIETCAP**" if source.startswith("vietcap-public:") else
                   "**Nguồn dữ liệu: CSV local**" if source.startswith("local-csv:") else
                   "**Nguồn dữ liệu thực đã cấu hình**")
    content = [f"# Backtest local: {result['symbol']} / {result['timeframe']}",
               "", source_note,
               "", f"Khoảng đánh giá: {result['first_evaluation']} đến {result['last_bar']}.",
               f"Số nến đầu vào: {result['bars']}; giao dịch hoàn tất: {result['closed_trades']}.",
               f"Lợi nhuận mô phỏng: {result['return_percent']:.2f}%; sụt giảm lớn nhất: {result['max_drawdown_percent']:.2f}%.",
               f"Tỷ lệ thắng: {win:.2f}%." if win is not None else "Tỷ lệ thắng: chưa có giao dịch hoàn tất để tính.",
               "", "## Giả định", "", *[f"- {key}: {value}" for key, value in result['assumptions'].items()],
               "", "## Giới hạn", "", *[f"- {value}" for value in result['limitations']]]
    md_path.write_text("\n".join(content) + "\n", encoding="utf-8")
    return md_path, json_path
