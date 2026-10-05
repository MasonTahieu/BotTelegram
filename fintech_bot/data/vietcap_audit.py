"""Offline coverage audit of the exact data the bot will consume."""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from fintech_bot.config import PROJECT_ROOT, load_settings
from fintech_bot.data.vietcap import VietcapCacheProvider
from fintech_bot.data.vietcap_http import atomic_json
from fintech_bot.domain import VIETNAM
from fintech_bot.market import MarketCalendar


def audit(directory, *, now=None, calendar=None, minimum=51, stale_after=600):
    now = now or datetime.now(VIETNAM)
    calendar = calendar or MarketCalendar()
    provider = VietcapCacheProvider(directory, calendar)
    rows, totals = [], Counter()
    for instrument in provider.instruments:
        symbol = instrument.symbol
        row = {"symbol": symbol, "exchange": instrument.exchange}
        frame = "1d"
        try:
            candles = [item for item in provider.get_historical_candles(symbol, frame)
                       if item.is_closed and item.timestamp <= now]
            last = candles[-1]
            expected = calendar.latest_end(now, instrument.exchange, frame)
            status = ("insufficient" if len(candles) < minimum else
                      "stale" if last.timestamp != expected else
                      "no_volume" if last.volume == 0 else "ready")
            row.update({frame + "_bars": len(candles), frame + "_last": last.timestamp.isoformat(),
                        frame + "_status": status, frame + "_error": ""})
        except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
            row.update({frame + "_bars": 0, frame + "_last": "", frame + "_status": "unavailable",
                        frame + "_error": str(error)})
        totals[frame + ":" + row[frame + "_status"]] += 1
        try:
            history = provider.get_financial_history(symbol)
            latest = provider.get_financials(symbol)
            missing = [field for field in ("eps", "pe", "pb", "roe", "debt_to_equity")
                       if getattr(latest, field) is None]
            status = "partial" if missing else "ready"
            row.update({"financials_status": status, "financials_periods": len(history),
                        "financials_latest": latest.period, "financials_missing": ",".join(missing),
                        "financials_published_on": latest.published_on.isoformat(),
                        "financials_observed_on": latest.observed_on.isoformat() if latest.observed_on else "",
                        "financials_error": ""})
        except (OSError, ValueError, TypeError, KeyError) as error:
            row.update({"financials_status": "unavailable", "financials_periods": 0,
                        "financials_latest": "", "financials_missing": "",
                        "financials_published_on": "", "financials_observed_on": "",
                        "financials_error": str(error)})
        totals["financials:" + row["financials_status"]] += 1
        rows.append(row)
        # Avoid retaining all raw reports in RAM during a one-pass audit.
        provider._cache.clear()
    return {"checked_at": now.isoformat(), "source": provider.source,
            "universe_counts": dict(Counter(x.exchange for x in provider.instruments)),
            "total_symbols": len(rows), "summary": dict(totals), "minimum_bars": minimum,
            "stale_after_seconds": stale_after, "rows": rows,
            "limitations": ["ready là đủ đầu vào theo bộ kiểm tra, không xác nhận quyền giao dịch của mã.",
                            "Chưa xác minh độ trễ/độ ổn định cập nhật trong phiên.",
                            "HNX có thể thiếu giao dịch sau 14:45; không tự tạo nến PLO.",
                            "Tỷ số tài chính là bản quan sát hiện tại; không phải dữ liệu point-in-time cho backtest."]}


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = "vietcap-coverage-" + report["checked_at"][:10]
    atomic_json(output / (stem + ".json"), report)
    with (output / (stem + ".csv")).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(report["rows"][0]))
        writer.writeheader()
        writer.writerows(report["rows"])
    lines = ["# Kiểm tra dữ liệu Vietcap", "", f"Thời điểm: {report['checked_at']}", "",
             f"Phạm vi: {report['total_symbols']} cổ phiếu; {report['universe_counts']}.", "",
             "| Hạng mục / trạng thái | Số mã |", "| --- | ---: |"]
    labels = {"ready": "đủ dữ liệu", "insufficient": "chưa đủ số nến", "stale": "nến cuối đã cũ",
              "no_volume": "nến cuối không có giao dịch", "unavailable": "thiếu hoặc không hợp lệ", "partial": "thiếu chỉ tiêu"}
    for key, count in sorted(report["summary"].items()):
        component, status = key.split(":")
        lines.append(f"| {component} — {labels[status]} | {count} |")
    lines += ["", "Chi tiết từng mã có trong file CSV và JSON cùng tên.", "",
              *["- " + item for item in report["limitations"]], ""]
    path = output / (stem + ".md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Kiểm tra bản dữ liệu Vietcap đã tải, không gọi mạng")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "vietcap.toml")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports")
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if settings.mode != "vietcap":
        parser.error("Cần cấu hình mode=vietcap")
    report = audit(settings.vietcap_path, calendar=MarketCalendar(settings.holidays),
                   minimum=settings.strategy.required_bars, stale_after=settings.stale_after_seconds)
    print(write_report(report, args.output))
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if any(key.endswith(":unavailable") for key in report["summary"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
