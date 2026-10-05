import argparse
import logging
from logging.handlers import RotatingFileHandler
import queue
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from fintech_bot.app import build_application
from fintech_bot.config import DEFAULT_CONFIG, PROJECT_ROOT, load_settings
from fintech_bot.data.validation import validate_candles, validate_market_times
from fintech_bot.data.base import FetchStatus
from fintech_bot.market import ReplayClock
from fintech_bot.services.backtest import run_backtest, write_backtest


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Fintech Bot — dữ liệu demo, CSV hoặc Vietcap đã đồng bộ")
    parser.add_argument("action", nargs="?", choices=("demo", "scan", "chat", "watch", "backtest", "live", "telegram", "setup-telegram"), default="demo")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--cycles", type=int, help="Số lượt kiểm tra lịch trong chế độ watch; bỏ trống để chạy liên tục")
    parser.add_argument("--fast", action="store_true", help="Tua nhanh đồng hồ demo khi watch")
    parser.add_argument("--console", action="store_true", help="Nhập token trong cửa sổ lệnh thay cho hộp thoại (setup-telegram)")
    parser.add_argument("--symbol", default="FPT", help="Mã dùng cho backtest")
    parser.add_argument("--timeframe", choices=("1d",), help="Khung backtest (chỉ hỗ trợ 1d)")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports")
    parser.add_argument("--start-date", type=date.fromisoformat, help="Ngày bắt đầu đánh giá backtest, YYYY-MM-DD; vẫn giữ lịch sử khởi tạo")
    parser.add_argument("--end-date", type=date.fromisoformat, help="Ngày cuối backtest, YYYY-MM-DD")
    args = parser.parse_args(argv)
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles phải lớn hơn 0")
    if args.console and args.action != "setup-telegram":
        parser.error("--console chỉ dùng với setup-telegram")
    if (args.start_date or args.end_date) and args.action != "backtest":
        parser.error("--start-date/--end-date chỉ dùng với backtest")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("Ngày bắt đầu phải trước hoặc bằng ngày kết thúc")
    try:
        if args.action == "setup-telegram":
            from fintech_bot.bot.telegram import setup_telegram
            return setup_telegram(console=args.console)
        settings = load_settings(args.config)
        if args.db is not None:
            settings = replace(settings, database_path=args.db.resolve())
        if args.timeframe is not None:
            settings = replace(settings, timeframe=args.timeframe)
        if args.fast and (settings.mode != "demo" or args.action != "watch"):
            raise ValueError("--fast chỉ dùng với watch ở chế độ demo.")
        log_directory = PROJECT_ROOT / "logs"
        log_directory.mkdir(exist_ok=True)
        logging.basicConfig(handlers=[RotatingFileHandler(log_directory / "app.log", maxBytes=2_000_000,
                                                          backupCount=3, encoding="utf-8")], level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        client = profile = None
        if args.action == "telegram":
            from fintech_bot.bot.telegram import TelegramClient, load_token
            if settings.mode == "demo":
                raise ValueError("Chạy Telegram với cấu hình Vietcap hoặc CSV; demo chỉ dùng local để tránh gửi nhầm dữ liệu giả lập.")
            client = TelegramClient(load_token())
            profile = client.verify()
            print(f"Python: {sys.executable} ({sys.version.split()[0]})")
            print(f"Cấu hình: {args.config.resolve()}")
            if args.db is None:
                settings = replace(settings, database_path=PROJECT_ROOT / "data" / f"telegram-{client.bot_id}.sqlite3")
        app = build_application(settings, notifier=client)
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        print(f"Không khởi động được: {error}", file=sys.stderr)
        return 1
    live_data = None
    try:
        if args.action in {"live", "telegram"} and settings.mode == "vietcap":
            from fintech_bot.services.live_data import LiveData
            live_data = LiveData(settings, app.scanner.instruments.values(), app.scanner.calendar)
            app.commands.live_data = live_data
        if args.action == "live" and live_data is None:
            raise ValueError("Chế độ live cần --config configs/vietcap.toml.")
        if args.action == "telegram":
            from fintech_bot.bot.telegram import run_telegram
            run_telegram(app, client, profile, live_data)
            return 0
        print({"demo": "DEMO LOCAL — Giá, khối lượng và tài chính đều giả lập.",
               "csv": "CSV LOCAL — Dữ liệu từ file bạn cung cấp, không gọi API.",
               "vietcap": "LIVE — Vnstock/KBS chính, Vietcap dự phòng; cache local được dùng để failover."}[settings.mode])
        if args.action == "backtest":
            symbol = args.symbol.upper()
            if symbol not in app.scanner.symbols:
                raise ValueError("Mã backtest không thuộc danh sách cấu hình.")
            batch_fetch = getattr(app.scanner.provider, "get_candles_batch", None)
            if hasattr(app.scanner.provider, "status_snapshot") and batch_fetch:
                batch = batch_fetch((symbol,), (settings.timeframe,))
                meta = batch.meta.get((symbol, settings.timeframe))
                if meta is None or meta.status is not FetchStatus.READY:
                    raise ValueError("Không có chuỗi READY để backtest.")
                candles = batch.candles[symbol, settings.timeframe]
                actual_source = meta.source
            else:
                fetch = getattr(app.scanner.provider, "get_historical_candles", app.scanner.provider.get_candles)
                candles = fetch(symbol, settings.timeframe)
                actual_source = app.scanner.provider.source
            # Only data available at the current application clock may be evaluated.
            candles = [item for item in candles if item.timestamp <= app.clock.now() and item.is_closed]
            if args.end_date:
                candles = [item for item in candles if item.session <= args.end_date]
            validate_candles(symbol, candles, app.scanner.strategy.required_bars + 1)
            validate_market_times(candles, app.scanner.calendar, app.scanner.instruments[symbol].exchange)
            result = run_backtest(symbol, candles, app.scanner.strategy, source=actual_source,
                                  is_demo=app.scanner.provider.is_demo, start_date=args.start_date)
            paths = write_backtest(result, args.output)
            for path in paths:
                print(f"Đã ghi báo cáo: {path.resolve()}")
            return 0
        app.alerts.register("local-demo")
        if args.action == "watch":
            count = 0
            had_errors = False
            print("Đang theo dõi theo lịch; Ctrl+C để dừng.")
            while args.cycles is None or count < args.cycles:
                result = app.scheduler.tick()
                had_errors = had_errors or result.errors > 0
                print(f"{app.clock.now():%d/%m %H:%M:%S}: {result.reason} Tin gửi: {result.sent}; lỗi: {result.errors}.")
                count += 1
                if args.cycles is not None and count >= args.cycles:
                    break
                delay = 0.02 if args.fast else settings.scan_interval_seconds
                time.sleep(delay)
                if isinstance(app.clock, ReplayClock):
                    app.clock.advance(settings.scan_interval_seconds)
            return 1 if had_errors else 0
        if args.action == "live":
            print(app.commands.handle("/status"))
        else:
            print(app.commands.handle("/scan"))
        app.scheduler.next_scan = app.clock.now().timestamp() + settings.scan_interval_seconds
        if args.action in {"demo", "scan"}:
            print(app.commands.handle("/signals"))
            return 1 if app.scanner.latest_report.errors else 0
        print(app.commands.handle("/start"))
        inbox = queue.Queue()
        def read_input():
            while True:
                line = sys.stdin.readline()
                inbox.put(line if line else None)
                if not line or line.strip().lower() == "/exit":
                    return
        threading.Thread(target=read_input, daemon=True).start()
        previous_tick = time.monotonic()
        print("\nBạn > ", end="", flush=True)
        while True:
            current_tick = time.monotonic()
            if isinstance(app.clock, ReplayClock):
                app.clock.advance(current_tick - previous_tick)
            previous_tick = current_tick
            if live_data:
                completed = live_data.tick()
                if completed is not None:
                    print(live_data.describe())
                if app.commands.consume_scan_request():
                    print("\n" + app.commands._scan())
                app.alerts.flush()
            else:
                app.scheduler.tick()
            try:
                line = inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None or line.strip().lower() == "/exit":
                print("\nĐã đóng phiên mô phỏng." if settings.mode == "demo" else "\nĐã đóng phiên local.")
                break
            print("\nBot > " + app.commands.handle(line))
            print("\nBạn > ", end="", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\nĐã dừng theo dõi.")
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Không thực hiện được: {error}", file=sys.stderr)
        return 1
    finally:
        if live_data:
            live_data.close()
        app.close()
