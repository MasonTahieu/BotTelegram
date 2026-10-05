"""Resumable closed-daily historical catch-up; never evaluates or sends signals."""

import argparse
from functools import wraps
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fintech_bot.config import PROJECT_ROOT, load_settings
from fintech_bot.data.daily_cache import LocalDailyCache
from fintech_bot.data.provider_manager import failure_category
from fintech_bot.data.universe import select_universe
from fintech_bot.data.validation import validate_candles, validate_market_times
from fintech_bot.data.vietcap import VietcapCacheProvider, fetched_at, parse_candles, parse_universe
from fintech_bot.data.vietcap_quotes import NoTradesYet, quote_candle
from fintech_bot.data.vietcap_http import VietcapClient, atomic_json, read_json
from fintech_bot.data.vnstock_provider import VnstockProvider
from fintech_bot.domain import VIETNAM
from fintech_bot.market import MarketCalendar
from fintech_bot.storage.lock import ProcessLock


def _collector_locked(function):
    """All production collectors share Vietcap's process lock."""
    @wraps(function)
    def locked(settings, *args, **kwargs):
        with ProcessLock(settings.vietcap_path / ".collector.lock"):
            return function(settings, *args, **kwargs)
    return locked


class BootstrapFailure(RuntimeError):
    def __init__(self, provider, error):
        self.provider = provider
        self.category = failure_category(error)
        if self.category == "provider_exception" and error.__cause__ is not None:
            self.category = failure_category(error.__cause__)
        self.original_type = type(error).__name__
        super().__init__(f"{provider}: {self.category}")


def _interleave(instruments):
    groups = {exchange: iter([item for item in instruments if item.exchange == exchange])
              for exchange in ("HOSE", "HNX", "UPCOM")}
    result = []
    while groups:
        for exchange in list(groups):
            item = next(groups[exchange], None)
            if item is None:
                del groups[exchange]
            else:
                result.append(item)
    return result


def _usable(cache, item, calendar, required_bars, now):
    candles, fetched, source = cache.read(item.symbol)
    closed = [candle for candle in candles if candle.is_closed]
    validate_candles(item.symbol, closed, required_bars)
    validate_market_times(closed, calendar, item.exchange)
    expected_end = calendar.latest_end(now, item.exchange, "1d")
    if fetched < expected_end:
        raise ValueError(
            f"Historical cache chưa được làm mới sau phiên đóng {expected_end.date()}."
        )
    if closed[-1].timestamp > expected_end:
        raise ValueError("Historical daily chứa nến đóng sau phiên gần nhất.")
    if closed[-1].timestamp < expected_end:
        raise ValueError("Historical cache thiếu phiên đóng gần nhất.")
    return closed, source


@_collector_locked
def bootstrap(settings, *, limit=None, symbols=(), retries=2, timeout=15, backoff=2,
              target_bars=250, resume=True, retry_failed=False, seconds_between_calls=0,
              progress=print, primary=None, backup=None, client=None, now=None):
    """Catch up the provider-neutral daily cache and checkpoint after every symbol."""
    calendar = MarketCalendar(settings.holidays)
    client = client or VietcapClient(interval=1, timeout=min(30, max(3, timeout)))
    if backup is None:
        universe_path = settings.vietcap_path / "universe.json"
        try:
            if not universe_path.exists():
                raise ValueError("Chưa có danh sách cổ phiếu Vietcap.")
            parse_universe(read_json(universe_path))
        except (OSError, ValueError, KeyError, TypeError):
            universe = client.universe()
            parse_universe(universe)
            atomic_json(universe_path, universe)
    backup = backup or VietcapCacheProvider(settings.vietcap_path, calendar)
    primary = primary or VnstockProvider(
        settings.vnstock_path, calendar, instruments=backup.instruments,
        required_bars=max(52, settings.strategy.required_bars),
        stale_after_seconds=settings.stale_after_seconds,
    )
    cache = LocalDailyCache(settings.vnstock_path.parent / "daily")
    required_bars = max(52, settings.strategy.required_bars)
    now = now or datetime.now(VIETNAM)
    instruments = select_universe(backup.instruments, settings.exchanges,
                                  tuple(symbol.upper() for symbol in symbols) or settings.symbols)
    instruments = _interleave(instruments)
    checkpoint_path = settings.vnstock_path / "bootstrap-progress.json"
    checkpoint = (read_json(checkpoint_path) if resume and checkpoint_path.exists()
                  else {"completed": {}, "failed": {}})
    completed = checkpoint.setdefault("completed", {})
    failed = checkpoint.setdefault("failed", {})
    if retry_failed:
        wanted = {key.split("/", 1)[0] for key in failed}
        instruments = [item for item in instruments if item.symbol in wanted]
    if limit:
        instruments = instruments[:limit]
    report = {
        "started_at": now.isoformat(), "finished_at": None, "selected": len(instruments),
        "target_bars": target_bars, "required_bars": required_bars,
        "ready": 0, "reused": 0, "no_new_candle": 0, "updated": 0, "bootstrapped": 0,
        "failed": 0, "sources": Counter(),
        "failure_groups": defaultdict(list), "failure_counts": Counter(),
        "results": {}, "provider_requests": {},
    }

    def attempt(call, provider_name):
        last = None
        for index in range(retries + 1):
            try:
                return call()
            except (Exception, SystemExit) as error:
                last = error
                if index < retries:
                    time.sleep(min(30, backoff * (2 ** index)))
        raise BootstrapFailure(provider_name, last) from last

    # One fresh quote batch can rule out a new candle only for a one-session gap.
    one_short = []
    for item in instruments:
        try:
            cached, _, _ = cache.read(item.symbol)
            validate_candles(item.symbol, cached, required_bars)
            validate_market_times(cached, calendar, item.exchange)
            expected = calendar.latest_end(now, item.exchange, "1d")
            if (cached[-1].timestamp < expected and
                    calendar.next_end(cached[-1].timestamp, item.exchange, "1d") == expected):
                one_short.append(item)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    no_new_candle = set()
    for offset in range(0, len(one_short), 100):
        batch = one_short[offset:offset + 100]
        try:
            envelope = client.quotes([item.symbol for item in batch])
            if not isinstance(envelope["data"], list):
                continue
            rows = {}
            for row in envelope["data"]:
                symbol = row.get("listingInfo", {}).get("symbol")
                if symbol in rows:
                    rows[symbol] = None
                elif symbol is not None:
                    rows[symbol] = row
            for item in batch:
                row = rows.get(item.symbol)
                expected = calendar.latest_end(now, item.exchange, "1d")
                if (row is None or fetched_at(envelope) < expected + timedelta(seconds=60) or
                        row.get("listingInfo", {}).get("tradingDate") != expected.date().isoformat()):
                    continue
                one = {**envelope, "data": row}
                try:
                    quote_candle(one, item.symbol, item.exchange, calendar)
                except NoTradesYet as error:
                    if error.session == expected.date():
                        no_new_candle.add(item.symbol)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    progress(f"Historical catch-up: {len(instruments)} mã, tối thiểu {required_bars} nến, bootstrap {target_bars}.")
    for index, item in enumerate(instruments, 1):
        key = item.symbol + "/1d"
        expected_end = calendar.latest_end(now, item.exchange, "1d")
        def validate_history(candles, fetched_at=None, *, allow_no_recent_trade=False):
            closed = [candle for candle in candles if candle.is_closed]
            validate_candles(item.symbol, closed, required_bars)
            validate_market_times(closed, calendar, item.exchange)
            expected_end = calendar.latest_end(now, item.exchange, "1d")
            if fetched_at is not None and fetched_at < expected_end:
                raise ValueError(
                    f"stale: nguồn chưa được refresh sau phiên đóng {expected_end.date()}."
                )
            if closed[-1].timestamp > expected_end:
                raise ValueError("invalid: historical daily chứa nến đóng sau phiên gần nhất.")
            if closed[-1].timestamp < expected_end and not allow_no_recent_trade:
                raise ValueError(
                    f"stale: historical daily dừng ở {closed[-1].session}; cần kiểm tra "
                    f"phiên đóng {expected_end.date()}."
                )
            return closed
        try:
            candles, source = _usable(cache, item, calendar, required_bars, now)
            report["reused"] += 1
            report["sources"][source] += 1
            report["results"][item.symbol] = "REUSED"
            completed[key] = {"source": source, "bars": len(candles),
                              "last": candles[-1].session.isoformat(), "at": now.isoformat()}
            failed.pop(key, None)
            if index % 25 == 0 or index == len(instruments):
                progress(f"{index}/{len(instruments)} READY {report['ready']}, REUSED {report['reused']}, ERROR {report['failed']}")
            continue
        except (OSError, ValueError, KeyError, TypeError):
            pass

        # Migrate usable provider caches before making any network request.
        # This makes resume fast and avoids re-downloading histories already on disk.
        migrated = False
        cached_sources = []
        if hasattr(primary, "get_cached_candles"):
            cached_sources.append((getattr(primary, "source", "VNSTOCK_KBS"),
                                   lambda: primary.get_cached_candles(item.symbol, "1d"), False))
        if hasattr(backup, "get_historical_result"):
            def load_backup_cache():
                result = backup.get_historical_result(item.symbol, "1d")
                if result.value is None or result.meta.status.value != "READY":
                    raise ValueError(result.error or result.meta.status.value)
                return result.value, result.meta.fetched_at
            cached_sources.append((getattr(backup, "source", "VIETCAP_DIRECT"),
                                   load_backup_cache, True))
        elif hasattr(backup, "get_historical_candles"):
            cached_sources.append((getattr(backup, "source", "VIETCAP_DIRECT"),
                                   lambda: (backup.get_historical_candles(item.symbol, "1d"), None), True))
        for cached_source, load, allow_no_recent_trade in cached_sources:
            try:
                cached_candles, cached_at = load()
                closed = validate_history(
                    cached_candles, cached_at,
                    allow_no_recent_trade=allow_no_recent_trade,
                )
                if closed[-1].timestamp != expected_end:
                    raise ValueError("Provider cache thiếu phiên đóng gần nhất.")
                cache.write(item.symbol, closed, cached_source, cached_at)
                completed[key] = {"source": cached_source, "bars": len(closed),
                                  "last": closed[-1].session.isoformat(),
                                  "at": datetime.now(VIETNAM).isoformat()}
                failed.pop(key, None)
                report["reused"] += 1
                report["sources"][cached_source] += 1
                report["results"][item.symbol] = "REUSED"
                migrated = True
                break
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if migrated:
            checkpoint["updated_at"] = datetime.now(VIETNAM).isoformat()
            checkpoint["summary"] = {"completed": len(completed), "failed": len(failed)}
            atomic_json(checkpoint_path, checkpoint)
            if index % 25 == 0 or index == len(instruments):
                progress(f"{index}/{len(instruments)} READY {report['ready']}, REUSED {report['reused']}, ERROR {report['failed']}")
            continue

        # A normal resume must continue past failures already attempted in this
        # day's interrupted bootstrap instead of spending network quota on them
        # again.  --retry-failed is the explicit command for retrying those
        # symbols.  Failures from an older day are not skipped because a later
        # trading session may have made fresh data available.
        previous_failure = failed.get(key) if resume and not retry_failed else None
        if previous_failure is not None:
            try:
                failed_at = datetime.fromisoformat(previous_failure.get("at", "")).astimezone(VIETNAM)
            except (TypeError, ValueError):
                failed_at = None
            if failed_at is not None and failed_at.date() == datetime.now(VIETNAM).date():
                group = previous_failure.get("group") or "provider_exception"
                report["failed"] += 1
                report["failure_groups"][group].append(item.symbol)
                bucket = {"missing_cache": "MISSING", "empty_response": "MISSING",
                          "stale_history": "STALE", "missing_recent_session": "STALE",
                          "insufficient_history": "INSUFFICIENT", "timeout": "TIMEOUT"}.get(
                              group, "PROVIDER_ERROR")
                report["failure_counts"][bucket] += 1
                report["results"][item.symbol] = "ERROR"
                if index % 25 == 0 or index == len(instruments):
                    progress(f"{index}/{len(instruments)} READY {report['ready']}, REUSED {report['reused']}, ERROR {report['failed']}")
                continue

        errors = []
        candles = source = fetched = None
        outcome = "BOOTSTRAPPED"
        try:
            cached, _, cached_source = cache.read(item.symbol)
            validate_candles(item.symbol, cached, required_bars)
            validate_market_times(cached, calendar, item.exchange)
            if cached[-1].timestamp >= expected_end:
                raise ValueError("Cache không thể cập nhật incremental.")
        except (OSError, ValueError, KeyError, TypeError):
            cached = None
        if cached is not None:
            missing = 0
            cursor = cached[-1].timestamp
            while cursor < expected_end:
                cursor = calendar.next_end(cursor, item.exchange, "1d")
                missing += 1
            if missing == 1 and item.symbol in no_new_candle:
                candles, source, fetched, outcome = cached, cached_source, now, "NO_NEW_CANDLE"
            else:
                try:
                    if cached_source == "VNSTOCK_KBS":
                        delta = attempt(
                            lambda: primary.get_history_through(
                                item.symbol, "1d", expected_end.date(), start=cached[-5].session),
                            "KBS",
                        )
                        fetched = datetime.now(VIETNAM)
                    else:
                        envelope = attempt(
                            lambda: client.candles(item.symbol, "1d", missing + 5,
                                                   to_timestamp=expected_end.timestamp()),
                            "VIETCAP",
                        )
                        fetched = datetime.fromisoformat(envelope["fetched_at"]).astimezone(VIETNAM)
                        delta = parse_candles(envelope, item.symbol, item.exchange, "1d", calendar)
                    by_day = {candle.session: candle for candle in cached}
                    by_day.update({candle.session: candle for candle in delta
                                   if candle.is_closed and candle.timestamp <= expected_end})
                    candles = validate_history(
                        sorted(by_day.values(), key=lambda candle: candle.timestamp), fetched,
                        allow_no_recent_trade=(cached_source == "VIETCAP_DIRECT"),
                    )
                    source, outcome = cached_source, "UPDATED"
                except (Exception, SystemExit) as error:
                    errors.append(error)
                    candles = None
        if candles is None and cached is None:
            try:
                fetched = datetime.now(VIETNAM)
                bounded = getattr(primary, "get_history_through", None)
                candles = validate_history(
                    attempt(lambda: bounded(item.symbol, "1d", expected_end.date())
                            if bounded else primary.get_candles(item.symbol, "1d"), "KBS"),
                    fetched,
                )
                source = getattr(primary, "source", "VNSTOCK_KBS")
            except (Exception, SystemExit) as error:
                errors.append(error)
                candles = None
        if candles is None and cached is None:
            try:
                # VietcapClient already owns HTTP timeout/retry/circuit-breaker.
                # A shorter outer thread timeout would abandon a request that is
                # still legitimately retrying and create orphan network calls.
                envelope = attempt(
                    lambda: client.candles(item.symbol, "1d", target_bars,
                                           to_timestamp=expected_end.timestamp()),
                    "VIETCAP",
                )
                fetched = datetime.fromisoformat(envelope["fetched_at"]).astimezone(VIETNAM)
                candles = validate_history(
                    parse_candles(envelope, item.symbol, item.exchange, "1d", calendar),
                    fetched,
                    allow_no_recent_trade=True,
                )
                atomic_json(settings.vietcap_path / f"{item.symbol}-daily.json", envelope)
                source = "VIETCAP_DIRECT"
            except (Exception, SystemExit) as error:
                errors.append(error)
        try:
            if candles is None:
                raise errors[-1]
            closed = validate_history(
                candles,
                fetched,
                allow_no_recent_trade=(source == "VIETCAP_DIRECT" or outcome == "NO_NEW_CANDLE"),
            )
            cache.write(item.symbol, closed, source, fetched)
            completed[key] = {"source": source, "bars": len(closed),
                              "last": closed[-1].session.isoformat(),
                              "at": datetime.now(VIETNAM).isoformat()}
            failed.pop(key, None)
            report["ready"] += 1
            report[{"UPDATED": "updated", "BOOTSTRAPPED": "bootstrapped",
                    "NO_NEW_CANDLE": "no_new_candle"}[outcome]] += 1
            report["sources"][source] += 1
            report["results"][item.symbol] = outcome
        except (Exception, SystemExit) as error:
            group = failure_category(error)
            entry = {"state": "ERROR", "group": group, "at": datetime.now(VIETNAM).isoformat(),
                     "providers": [failure_category(value) for value in errors] or [group]}
            failed[key] = entry
            completed.pop(key, None)
            report["failed"] += 1
            report["failure_groups"][group].append(item.symbol)
            bucket = {"missing_cache": "MISSING", "empty_response": "MISSING",
                      "stale_history": "STALE", "missing_recent_session": "STALE",
                      "insufficient_history": "INSUFFICIENT", "timeout": "TIMEOUT"}.get(
                          group, "PROVIDER_ERROR")
            report["failure_counts"][bucket] += 1
            report["results"][item.symbol] = "ERROR"
        checkpoint["updated_at"] = datetime.now(VIETNAM).isoformat()
        checkpoint["summary"] = {"completed": len(completed), "failed": len(failed)}
        atomic_json(checkpoint_path, checkpoint)
        if index % 25 == 0 or index == len(instruments):
            progress(f"{index}/{len(instruments)} READY {report['ready']}, REUSED {report['reused']}, ERROR {report['failed']}")
        if seconds_between_calls:
            time.sleep(seconds_between_calls)

    report["finished_at"] = datetime.now(VIETNAM).isoformat()
    report["sources"] = dict(report["sources"])
    report["failure_groups"] = dict(report["failure_groups"])
    report["failure_counts"] = {name: report["failure_counts"].get(name, 0) for name in
                                ("MISSING", "STALE", "INSUFFICIENT", "TIMEOUT", "PROVIDER_ERROR")}
    report["universe"] = len(instruments)
    report["provider_requests"] = {
        "kbs": getattr(primary, "api_call_count", None),
        "vietcap": getattr(client, "requests", None),
    }
    checkpoint["finished_at"] = report["finished_at"]
    checkpoint["summary"] = {"completed": len(completed), "failed": len(failed)}
    atomic_json(checkpoint_path, checkpoint)
    atomic_json(settings.vnstock_path / "bootstrap-last-run.json", report)
    return report


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description="Catch up historical daily KBS → Vietcap, có checkpoint/resume")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "vietcap.toml")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--backoff", type=float, default=2)
    parser.add_argument("--bars", type=int, default=250)
    parser.add_argument("--seconds-between-calls", type=float, default=0)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit phải lớn hơn 0")
    if args.retries < 0 or not 3 <= args.timeout <= 60 or args.backoff < 0:
        parser.error("retries/backoff không âm; timeout từ 3 đến 60 giây")
    if not 52 <= args.bars <= 5000:
        parser.error("--bars phải từ 52 đến 5000")
    settings = load_settings(args.config)
    try:
        report = bootstrap(
            settings, limit=args.limit, symbols=tuple(args.symbols), retries=args.retries,
            timeout=args.timeout, backoff=args.backoff, target_bars=args.bars,
            resume=not args.no_resume, retry_failed=args.retry_failed,
            seconds_between_calls=args.seconds_between_calls,
            progress=lambda message: print(message, flush=True),
        )
    except ValueError as error:
        print(f"Không chạy được historical catch-up: {error}", file=sys.stderr)
        return 2
    print(f"Universe: {report['universe']}\nREUSED: {report['reused']}\n"
          f"NO_NEW_CANDLE: {report['no_new_candle']}\nUPDATED: {report['updated']}\n"
          f"BOOTSTRAPPED: {report['bootstrapped']}\nERROR: {report['failed']}\n"
          f"READY: {report['ready'] + report['reused']}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
