from fintech_bot.domain import Signal, SignalSide

LABELS = {
    SignalSide.BUY: "MUA",
    SignalSide.SELL: "BÁN",
    SignalSide.NONE: "KHÔNG CÓ TÍN HIỆU MỚI",
}


def source_label(source: str, *, is_demo=False) -> str:
    if is_demo:
        return "DEMO"
    if source == "VNSTOCK_KBS":
        return "Vnstock / KBS"
    if source == "VIETCAP_DIRECT":
        return "Vietcap Direct"
    if str(source).startswith("LAST_KNOWN_GOOD:"):
        return "Dữ liệu gần nhất (stale)"
    if source == "MULTI_PROVIDER":
        return "Vnstock/KBS + Vietcap failover"
    if str(source).startswith("vietcap-public:"):
        return "Vietcap"
    if str(source).startswith(("csv", "local-csv:")):
        return "CSV local"
    # Internal source ids may contain paths. Unknown real sources stay generic.
    return "Nguồn dữ liệu đã cấu hình"


def format_price(value) -> str:
    """Compact price label without dropping meaningful decimal digits."""
    if value is None:
        return "chưa có"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def format_signal(signal: Signal) -> str:
    """Present every indicator supplied by the existing strategy."""
    label = "[DEMO • dữ liệu giả lập]" if signal.is_demo else "[Tín hiệu]"
    marker = {SignalSide.BUY: "🟢", SignalSide.SELL: "🔴",
              SignalSide.NONE: "🟡"}[signal.side]
    timestamp = signal.closed_at.strftime('%d/%m/%Y %H:%M %z') if signal.closed_at else str(signal.session)
    lines = [
        f"{marker} {signal.symbol}: {LABELS[signal.side]}  {label}",
        f"{signal.exchange or 'chưa xác định'}  ·  {signal.timeframe.upper()}  ·  ĐÃ XÁC NHẬN",
        "",
        "PHIÊN GIAO DỊCH",
        f"Phiên dữ liệu  {signal.session:%d/%m/%Y}",
        f"Mốc đóng nến  {timestamp}",
    ]
    if signal.observed_at:
        lines.append(f"Thời điểm quan sát  {signal.observed_at:%d/%m/%Y %H:%M:%S %z}")
    lines.extend(["", "Lý do", signal.reason, "", "CHỈ BÁO TẠI PHIÊN ĐÓNG"])
    names = {"close": "Giá đóng", "ema_fast": "EMA nhanh",
             "ema_slow": "EMA chậm", "rsi": "RSI"}
    for name, value in signal.indicators.items():
        display = format_price(value) if name in {"close", "ema_fast", "ema_slow"} else f"{value:,.2f}"
        lines.append(f"{names.get(name, name)}  {display}")
    lines.extend([
        "",
        f"Chiến lược  {signal.strategy_id}",
        f"Nguồn: {source_label(signal.source, is_demo=signal.is_demo)}",
    ])
    return "\n".join(lines)
