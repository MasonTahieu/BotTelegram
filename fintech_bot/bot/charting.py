"""Presentation-only chart renderer for prepared, closed-daily data."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from fintech_bot.bot.formatting import format_price, source_label


WIDTH, HEIGHT = 1280, 1280
CANVAS = "#F8FAFC"
SURFACE = "#FFFFFF"
INK = "#0F172A"
SECONDARY = "#475569"
MUTED = "#64748B"
BORDER = "#CBD5E1"
GRID = "#E2E8F0"
BLUE = "#2563EB"
AMBER = "#D97706"
PURPLE = "#7C3AED"
TEAL = "#0F766E"
GREEN = "#15803D"
RED = "#DC2626"


def _font(size, bold=False):
    names = (("C:/Windows/Fonts/segoeuib.ttf", "DejaVuSans-Bold.ttf") if bold else
             ("C:/Windows/Fonts/segoeui.ttf", "DejaVuSans.ttf"))
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _price_ticks(low, high, count=5):
    """Choose readable axis labels without altering plotted values."""
    raw = max(high - low, 1) / max(count - 1, 1)
    magnitude = 10 ** (len(str(int(abs(raw)))) - 1) if raw >= 1 else 0.001
    step = next((unit * magnitude for unit in (1, 2, 2.5, 5, 10)
                 if unit * magnitude >= raw), 10 * magnitude)
    value = int(low // step) * step
    ticks = []
    while value <= high + step * .1:
        if value >= low - step * .1:
            ticks.append(value)
        value += step
    return ticks[:7]


def render_chart_image(path, symbol, exchange, source, visible, closes, fast, slow,
                       strength, offset, signal, ema_fast, ema_slow, rsi_period,
                       is_demo=False):
    """Only draw the candles, indicators and signal supplied by telegram.py."""
    count = len(visible)
    image = Image.new("RGB", (WIDTH, HEIGHT), CANVAS)
    draw = ImageDraw.Draw(image)
    title_font, body_font = _font(34, True), _font(19)
    small_font, metric_font = _font(17), _font(19, True)
    draw.rounded_rectangle((36, 28, 1244, 1250), radius=16,
                           fill=SURFACE, outline=BORDER, width=1)
    draw.text((76, 68), f"{symbol} · PHÂN TÍCH KỸ THUẬT 1D",
              fill=INK, font=title_font)
    period = f"{visible[0].session:%d/%m/%Y} – {visible[-1].session:%d/%m/%Y}"
    draw.text((76, 116), f"{count} nến đã đóng · {exchange} · {period}",
              fill=MUTED, font=body_font)
    draw.text((76, 148), f"Nguồn historical: {source_label(source, is_demo=is_demo)}",
              fill=MUTED, font=small_font)
    for left, color, label in ((76, BLUE, "Giá đóng"), (290, AMBER, f"EMA {ema_fast}"),
                               (500, PURPLE, f"EMA {ema_slow}"),
                               (710, TEAL, f"RSI {rsi_period}")):
        draw.line((left, 213, left + 24, 213), fill=color, width=4)
        draw.text((left + 32, 202), label, fill=SECONDARY, font=small_font)

    plot_left, plot_right = 96, 1086
    price_top, price_bottom = 280, 790
    rsi_top, rsi_bottom = 900, 1080
    values = [float(value) for series in (closes, fast, slow)
              for value in series[offset:] if value is not None]
    low, high = min(values), max(values)
    pad = max((high - low) * .08, abs(high) * .002, 1)
    low, high = low - pad, high + pad
    x = lambda index: round(plot_left + (plot_right - plot_left) * index / max(1, count - 1))
    y = lambda value: round(price_bottom - (float(value) - low) * (price_bottom - price_top) / (high - low))
    yr = lambda value: round(rsi_bottom - float(value) * (rsi_bottom - rsi_top) / 100)

    draw.text((plot_left, price_top - 31), "GIÁ ĐÓNG CỬA", fill=SECONDARY, font=small_font)
    for value in _price_ticks(low, high):
        yy = y(value)
        draw.line((plot_left, yy, plot_right, yy), fill=GRID, width=1)
        draw.text((plot_right + 18, yy - 10), format_price(value),
                  fill=MUTED, font=small_font)
    for index in sorted({0, count // 4, count // 2, 3 * count // 4, count - 1}):
        xx = x(index)
        draw.line((xx, price_top, xx, rsi_bottom), fill="#F1F5F9", width=1)
        draw.text((xx - 23, rsi_bottom + 26), visible[index].session.strftime("%d/%m"),
                  fill=MUTED, font=small_font)
    draw.rectangle((plot_left, price_top, plot_right, price_bottom),
                   outline=BORDER, width=1)

    def line(series, color, width, mapper):
        points = [(x(index), mapper(value)) for index, value in
                  enumerate(series[offset:]) if value is not None]
        if len(points) > 1:
            draw.line(points, fill=color, width=width, joint="curve")

    line(closes, BLUE, 4, y)
    line(fast, AMBER, 3, y)
    line(slow, PURPLE, 3, y)
    if signal:
        index = next((i for i, candle in enumerate(visible)
                      if candle.session == signal.session), None)
        if index is not None:
            xx, yy = x(index), y(visible[index].close)
            side = signal.side.value
            if side == "BUY":
                draw.polygon(((xx, yy - 18), (xx - 10, yy), (xx + 10, yy)), fill=GREEN)
                draw.text((min(xx + 18, plot_right - 70), yy - 43), "MUA",
                          fill=GREEN, font=metric_font)
            elif side == "SELL":
                draw.polygon(((xx, yy + 18), (xx - 10, yy), (xx + 10, yy)), fill=RED)
                draw.text((min(xx + 18, plot_right - 70), yy + 22), "BÁN",
                          fill=RED, font=metric_font)

    draw.text((plot_left, rsi_top - 31), f"RSI {rsi_period}",
              fill=SECONDARY, font=small_font)
    draw.rectangle((plot_left, rsi_top, plot_right, yr(70)), fill="#FFFBEB")
    for level in (30, 50, 70):
        yy = yr(level)
        draw.line((plot_left, yy, plot_right, yy), fill=GRID, width=1)
        draw.text((plot_right + 18, yy - 10), str(level), fill=MUTED, font=small_font)
    line(strength, TEAL, 3, yr)
    draw.rectangle((plot_left, rsi_top, plot_right, rsi_bottom),
                   outline=BORDER, width=1)

    latest, prior = visible[-1].close, visible[-2].close if count > 1 else None
    change = (latest / prior - 1) * 100 if prior else None
    change_text = f"{change:+.2f}%" if change is not None else "—"
    change_color = GREEN if change is not None and change >= 0 else RED
    latest_rsi = strength[-1]
    rsi_text = f"{latest_rsi:.2f}" if latest_rsi is not None else "chưa có"
    side = signal.side.value if signal else ""
    signal_text = "MUA" if side == "BUY" else "BÁN" if side == "SELL" else "Chưa có tín hiệu mới"
    draw.line((76, 1134, 1204, 1134), fill=GRID, width=1)
    draw.text((76, 1155), f"Đóng cửa  {format_price(latest)}",
              fill=INK, font=metric_font)
    draw.text((350, 1155), f"Biến động  {change_text}",
              fill=change_color, font=metric_font)
    draw.text((625, 1155), f"RSI  {rsi_text}",
              fill=INK, font=metric_font)
    draw.text((830, 1155), f"Tín hiệu  {signal_text}",
              fill=INK, font=metric_font)
    draw.text((76, 1203),
              "Chỉ dùng nến đã đóng · Quote trong phiên không được đưa vào EMA hoặc RSI",
              fill=MUTED, font=small_font)
    image.save(Path(path), format="PNG", optimize=True)
