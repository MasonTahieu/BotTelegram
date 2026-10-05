"""Explicit, resumable download job. Run separately from the local bot."""

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path

from fintech_bot.config import PROJECT_ROOT, load_settings
from fintech_bot.data.vietcap import fetched_at, parse_universe, _raw_prices
from fintech_bot.data.vietcap_http import VietcapClient, SourceUnavailable, atomic_json, read_json
from fintech_bot.domain import VIETNAM
from fintech_bot.market import MarketCalendar
from fintech_bot.storage.lock import ProcessLock


def collect(directory, **kwargs):
    with ProcessLock(Path(directory) / ".collector.lock"):
        return _collect(directory, **kwargs)


def _collect(directory, *, components=("daily", "ratios", "income"), symbols=(), limit=None,
            resume=False, max_age=86400, daily_bars=250, client=None, progress=print, workers=3,
            exchanges=("HOSE", "HNX", "UPCOM")):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    client = client or VietcapClient()
    started = datetime.now(VIETNAM)
    components = tuple(dict.fromkeys(components))
    if not components or set(components) - {"daily", "ratios", "income"}:
        raise ValueError("Chỉ hỗ trợ dữ liệu daily, ratios và income; minute đã ngừng hoạt động.")
    financial_only = set(components).issubset({"ratios", "income"})
    def reusable(path, component=None):
        if not resume or not path.exists():
            return False
        try:
            envelope = read_json(path)
            age = (datetime.now(VIETNAM) - fetched_at(envelope)).total_seconds()
            if component == "daily":
                requested = daily_bars
                existing = envelope.get("requested_bars", len(envelope["data"][0]["t"]))
                if existing < requested:
                    return False
            return 0 <= age <= max_age
        except (ValueError, KeyError, TypeError):
            return False
    universe_path = directory / "universe.json"
    if reusable(universe_path):
        universe = read_json(universe_path)
    else:
        universe = client.universe()
        parse_universe(universe)
        atomic_json(universe_path, universe)
    instruments = parse_universe(universe)
    instruments = [item for item in instruments if item.exchange in exchanges]
    if symbols:
        unknown = set(symbols) - {x.symbol for x in instruments}
        if unknown:
            raise ValueError("Mã không có trong danh sách Vietcap: " + ", ".join(sorted(unknown)))
        instruments = [x for x in instruments if x.symbol in symbols]
    # Interleave exchanges so an interrupted run still covers all three markets.
    by_exchange = {exchange: iter([x for x in instruments if x.exchange == exchange])
                   for exchange in ("HOSE", "HNX", "UPCOM")}
    ordered = []
    while by_exchange:
        for exchange in list(by_exchange):
            item = next(by_exchange[exchange], None)
            if item is None:
                del by_exchange[exchange]
            else:
                ordered.append(item)
    instruments = ordered
    if limit:
        instruments = instruments[:limit]
    failure_path = directory / "failures.json"
    failures = read_json(failure_path) if failure_path.exists() else {}
    report = {"started_at": started.isoformat(), "finished_at": None,
              "universe_counts": dict(Counter(x.exchange for x in parse_universe(universe))),
              "selected_symbols": len(instruments), "components": list(components),
              "downloaded": 0, "reused": 0, "errors": {}, "stopped": None,
              "interval_seconds": client.interval, "results": {}, "network_pauses": 0}
    if not 1 <= workers <= 4:
        raise ValueError("Số tác vụ đồng thời phải từ 1 đến 4.")
    total = len(instruments) * len(components)
    completed, consecutive_errors, consecutive_network_errors = 0, 0, 0
    failed_at_start = set(failures)
    tasks = iter((item, component) for item in instruments for component in components)

    def download(item, component):
        name = item.symbol + "-" + component
        path = directory / (name + ".json")
        if reusable(path, component) and name not in failed_at_start:
            return "reused"
        if component == "daily":
            envelope = client.candles(item.symbol, "1d", daily_bars)
            if not _raw_prices(envelope, item.symbol):
                raise ValueError("Nguồn không trả nến.")
        else:
            envelope = client.financials(item.symbol, component)
            rows = envelope["data"].get("data")
            if not rows or not isinstance(rows, list if component == "ratios" else dict):
                raise ValueError("Nguồn không trả bảng tài chính.")
        atomic_json(path, envelope)
        return "downloaded"

    progress(f"Danh sách: {len(instruments)} mã; {total} phần dữ liệu; cách nhau tối thiểu {client.interval}s trên toàn bộ tác vụ.")
    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    def submit_next():
        if (directory / "stop-collection.flag").exists():
            report["stopped"] = "Đã nhận yêu cầu dừng; dữ liệu đã tải được giữ lại để tiếp tục."
            return
        task = next(tasks, None)
        if task is not None:
            item, component = task
            pending[executor.submit(download, item, component)] = item.symbol + "-" + component
    try:
        for _ in range(workers):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                name = pending.pop(future)
                try:
                    status = future.result()
                    report[status] += 1
                    report["results"][name] = status
                    failures.pop(name, None)
                    consecutive_errors = consecutive_network_errors = 0
                except (OSError, ValueError, TypeError, KeyError) as error:
                    message = str(error)
                    report["errors"][name] = message
                    report["results"][name] = "error"
                    failures[name] = {"at": datetime.now(VIETNAM).isoformat(), "error": message}
                    consecutive_errors += 1
                    transient_network = (isinstance(error, OSError) or
                                         any(term in message.lower() for term in
                                             ("timeout", "timed out", "kết nối", "connection", "network", "dns")))
                    consecutive_network_errors = (consecutive_network_errors + 1
                                                  if transient_network else 0)
                    if isinstance(error, SourceUnavailable):
                        report["stopped"] = message
                    elif not financial_only and consecutive_errors >= 10:
                        report["stopped"] = message
                    elif financial_only and consecutive_network_errors >= 10:
                        if report["network_pauses"] >= 2:
                            report["stopped"] = message
                        else:
                            report["network_pauses"] += 1
                            report["requests"] = client.requests
                            atomic_json(directory / "sync-progress.json", report)
                            progress("10 lỗi mạng liên tiếp; nghỉ 30 giây rồi tiếp tục phần tài chính "
                                     f"chưa thử ({report['network_pauses']}/2 lần nghỉ).")
                            time.sleep(30)
                            consecutive_errors = consecutive_network_errors = 0
                completed += 1
                atomic_json(failure_path, failures)
                if completed % 50 == 0 or completed == total:
                    report["requests"] = client.requests
                    atomic_json(directory / "sync-progress.json", report)
                    progress(f"{completed}/{total}: tải {report['downloaded']}, dùng lại {report['reused']}, lỗi {len(report['errors'])}.")
                if not report["stopped"]:
                    submit_next()
        if report["stopped"]:
            progress("Đã dừng: " + report["stopped"])
    except KeyboardInterrupt:
        report["stopped"] = "Người dùng dừng; lần sau có thể chạy --resume."
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        report["finished_at"] = datetime.now(VIETNAM).isoformat()
        report["requests"] = client.requests
        report["unattempted"] = total - len(report["results"])
        atomic_json(failure_path, failures)
        atomic_json(directory / "sync-progress.json", report)
        atomic_json(directory / ("sync-" + started.strftime("%Y%m%d-%H%M%S") + ".json"), report)
    return report


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description="Tải dữ liệu thị trường công khai Vietcap; không cần tài khoản.")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "vietcap.toml")
    parser.add_argument("--directory", type=Path, help="Đổi thư mục tải; mặc định data.vietcap_path trong cấu hình")
    parser.add_argument("--components", nargs="+", choices=("daily", "ratios", "income"),
                        default=["daily", "ratios", "income"])
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=10, help="Thời gian chờ một yêu cầu, 3–30 giây")
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-age", type=int, default=86400, help="Tuổi bản tải tối đa khi --resume, giây")
    parser.add_argument("--daily-bars", type=int, default=250)
    parser.add_argument("--watch", action="store_true", help="Sau lượt đầu, tiếp tục tải giá trong phiên; Ctrl+C để dừng")
    parser.add_argument("--refresh-seconds", type=int, default=300, help="Khoảng nghỉ sau khi một lượt tải trong phiên hoàn tất")
    args = parser.parse_args(argv)
    if (args.limit is not None and args.limit < 1) or args.max_age < 0:
        parser.error("limit phải dương; max-age không âm")
    if not 51 <= args.daily_bars <= 5000:
        parser.error("daily-bars phải từ 51 đến 5000")
    if args.refresh_seconds < 30:
        parser.error("refresh-seconds tối thiểu 30 giây")
    if not 3 <= args.timeout <= 30:
        parser.error("timeout phải từ 3 đến 30 giây")
    try:
        settings = load_settings(args.config)
        if settings.mode != "vietcap":
            raise ValueError("Bộ tải cần cấu hình mode=vietcap.")
        args.directory = args.directory or settings.vietcap_path
        calendar = MarketCalendar(settings.holidays)
        components = tuple(dict.fromkeys(args.components))
        resume = args.resume
        while True:
            report = collect(args.directory, components=components,
                             symbols=tuple(x.upper() for x in args.symbols) or settings.symbols,
                             exchanges=settings.exchanges, limit=args.limit, resume=resume,
                             max_age=args.max_age, daily_bars=args.daily_bars,
                             client=VietcapClient(interval=args.interval, timeout=args.timeout), progress=lambda x: print(x, flush=True), workers=args.workers)
            print(f"Báo cáo: {args.directory.resolve() / 'sync-progress.json'}")
            print(f"Phần dữ liệu: tải {report['downloaded']}, dùng lại {report['reused']}, "
                  f"lỗi {len(report['errors'])}, chưa thử {report['unattempted']}.")
            if not args.watch or report["stopped"]:
                return 1 if report["errors"] or report["stopped"] else 0
            # Financial reports are fetched in the first pass only.
            components = tuple(x for x in components if x == "daily")
            if not components:
                return 1 if report["errors"] else 0
            resume = False
            print("Đợi lượt tải giá tiếp theo trong phiên. Độ dài lượt tải phụ thuộc số mã và nguồn.")
            time.sleep(args.refresh_seconds)
            while not calendar.is_scan_time(datetime.now(VIETNAM)):
                time.sleep(30)
    except KeyboardInterrupt:
        print("Đã dừng bộ tải dữ liệu.")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Không thu thập được: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
