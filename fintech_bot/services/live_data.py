"""One bounded background updater; the application thread owns all SQLite work."""

import queue
import threading
import time
from collections import Counter
from datetime import datetime, time as wall_time, timedelta

from fintech_bot.data.readiness import ComponentHealth, ReadinessState
from fintech_bot.data.vietcap import fetched_at, parse_financials
from fintech_bot.data.vietcap_http import VietcapClient, SourceUnavailable, atomic_json, read_json
from fintech_bot.data.vietcap_quotes import quote_candle, NoTradesYet
from fintech_bot.domain import VIETNAM
from fintech_bot.storage.lock import ProcessLock


COMPONENTS = {"ratios", "income", "quote"}
USER_PRIORITY, WATCHLIST_PRIORITY, ALERT_PRIORITY, BACKGROUND_PRIORITY = range(4)


def _error_category(error):
    text = str(error).lower()
    if isinstance(error, SourceUnavailable):
        return "source_blocked"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "429" in text:
        return "rate_limit"
    if "chưa xác nhận mã được giao dịch" in text:
        return "source_unconfirmed"
    if "json" in text or "định dạng" in text or "không hợp lệ" in text:
        return "invalid_data"
    return "fetch_error"


def _record_failure(failures, name, error):
    previous = failures.get(name, {})
    attempts = int(previous.get("attempts", 0)) + 1
    delay = max(getattr(error, "retry_after", 0), min(300, 5 * (2 ** min(attempts - 1, 6))))
    category = _error_category(error)
    if category == "source_unconfirmed":
        delay = max(delay, 3600)
    now = datetime.now(VIETNAM)
    failures[name] = {
        "at": now.isoformat(),
        "error": str(error),
        "category": category,
        "attempts": attempts,
        "retry_at": (now + timedelta(seconds=delay)).isoformat(),
    }


def refresh_quotes(directory, instruments, calendar, client, progress=None, stop=None):
    """Persist each successful batch, and explicitly invalidate missing/failed symbols."""
    failures_path = directory / "failures.json"
    failures = read_json(failures_path) if failures_path.exists() else {}
    requests_before = getattr(client, "requests", 0)
    report = {"started_at": datetime.now(VIETNAM).isoformat(), "requested": len(instruments),
              "received": 0, "valid": 0, "waiting": 0, "errors": {}, "statuses": {},
              "status_counts": {}, "error_groups": {}, "finished_at": None,
              "provider_requests": 0}
    atomic_json(directory / "quotes-status.json", report)
    try:
        for index in range(0, len(instruments), 100):
            if stop and stop.is_set():
                break
            batch = instruments[index:index+100]
            try:
                envelope = client.quotes([item.symbol for item in batch])
                if not isinstance(envelope["data"], list):
                    raise ValueError("Bảng giá không trả danh sách.")
                atomic_json(directory / "quote-batches" / f"batch-{index // 100:02}.json", envelope)
                rows = {}
                for row in envelope["data"]:
                    symbol = row.get("listingInfo", {}).get("symbol")
                    if symbol in rows:
                        raise ValueError("Lô bảng giá trả trùng mã.")
                    rows[symbol] = row
                report["received"] += sum(item.symbol in rows for item in batch)
                batch_error = None
            except (OSError, ValueError, KeyError, TypeError) as error:
                rows, batch_error = {}, error
            for item in batch:
                name = item.symbol + "-quote"
                try:
                    if batch_error:
                        raise batch_error
                    if item.symbol not in rows:
                        report["statuses"][item.symbol] = "MISSING"
                        raise ValueError("Lô bảng giá thiếu mã yêu cầu.")
                    one = {**envelope, "data": rows[item.symbol]}
                    quote_candle(one, item.symbol, item.exchange, calendar)
                    atomic_json(directory / (name + ".json"), one)
                    failures.pop(name, None)
                    report["valid"] += 1
                    report["statuses"][item.symbol] = "READY"
                except NoTradesYet:
                    atomic_json(directory / (name + ".json"), one)
                    failures.pop(name, None)
                    report["waiting"] += 1
                    report["statuses"][item.symbol] = "NO_TRADE"
                except (OSError, ValueError, KeyError, TypeError) as error:
                    report["errors"][item.symbol] = str(error)
                    if item.symbol not in report["statuses"]:
                        report["statuses"][item.symbol] = "ERROR" if batch_error else "INVALID"
                    _record_failure(failures, name, error)
            counts = Counter(report["statuses"].values())
            report["status_counts"] = {key: counts.get(key, 0) for key in
                                       ("READY", "NO_TRADE", "MISSING", "INVALID", "ERROR")}
            groups = Counter(_error_category(ValueError(value)) for value in report["errors"].values())
            report["error_groups"] = dict(groups)
            atomic_json(failures_path, failures)
            atomic_json(directory / "quotes-status.json", report)
            if progress:
                progress(dict(report))
            if isinstance(batch_error, SourceUnavailable):
                raise batch_error
    finally:
        report["finished_at"] = datetime.now(VIETNAM).isoformat()
        report["provider_requests"] = max(0, getattr(client, "requests", requests_before) - requests_before)
        atomic_json(directory / "quotes-status.json", report)
    return report


class LiveData:
    def __init__(self, settings, instruments, calendar, client=None):
        self.settings, self.directory = settings, settings.vietcap_path
        self.instruments, self.calendar = list(instruments), calendar
        self.by_symbol = {item.symbol: item for item in self.instruments}
        self.client = client or VietcapClient(interval=1, timeout=10)
        self.stop = threading.Event()
        self.results = queue.Queue(maxsize=1)
        self.thread = None
        self.next_run = 0.0
        self.requested = True  # One initial refresh, including outside sessions.
        self.priority = {}
        self._sequence = 0
        self._condition = threading.Condition()
        self._states = {}
        self._inflight = set()
        self.latest = None
        self.started = None
        self.started_monotonic = 0.0
        self.blocked = False

    def request_refresh(self):
        self.requested = True

    @staticmethod
    def components_for_timeframe(timeframe):
        if timeframe == "1d":
            return ("quote",)
        raise ValueError("Bot chỉ hỗ trợ khung nến 1d.")

    def _failure_retry_pending(self, name):
        path = self.directory / "failures.json"
        try:
            failure = (read_json(path) if path.exists() else {}).get(name)
            retry_at = datetime.fromisoformat(failure.get("retry_at")) if failure and failure.get("retry_at") else None
            return bool(retry_at and retry_at > datetime.now(VIETNAM))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def request_components(self, symbol, components, priority=USER_PRIORITY):
        if symbol not in self.by_symbol:
            return
        with self._condition:
            for component in components:
                if component not in COMPONENTS:
                    raise ValueError("Component dữ liệu không hợp lệ.")
                key = (symbol, component)
                if key in self._inflight:
                    continue
                current = self.priority.get(key)
                if current is None or priority < current[0]:
                    self._sequence += 1
                    self.priority[key] = (priority, self._sequence)
            self.requested = True
            self._condition.notify_all()

    def request_symbol(self, symbol, timeframe, priority=USER_PRIORITY):
        self.request_components(symbol, self.components_for_timeframe(timeframe), priority)

    def _take_priority(self):
        with self._condition:
            tasks = sorted(self.priority, key=lambda key: (*self.priority[key], key))
            self.priority = {}
            for symbol, component in tasks:
                self._inflight.add((symbol, component))
                self._states[symbol, component] = ComponentHealth(ReadinessState.FETCHING, "Đang tải.")
            self._condition.notify_all()
            return tasks

    def _set_state(self, symbol, component, health):
        with self._condition:
            self._states[symbol, component] = health
            self._condition.notify_all()

    def component_health(self, symbol, component, *, ignore_fetching=False):
        if symbol not in self.by_symbol or component not in COMPONENTS:
            return ComponentHealth(ReadinessState.INVALID, "Mã hoặc component không hợp lệ.")
        key, name = (symbol, component), f"{symbol}-{component}"
        with self._condition:
            current = self._states.get(key)
            if not ignore_fetching and current and current.state is ReadinessState.FETCHING:
                return current
        path = self.directory / (name + ".json")
        failures_path = self.directory / "failures.json"
        try:
            failures = read_json(failures_path) if failures_path.exists() else {}
            failure = failures.get(name)
            if failure:
                failed_at = failure.get("at")
                try:
                    failed_stamp = datetime.fromisoformat(failed_at).timestamp() if failed_at else float("inf")
                except (TypeError, ValueError):
                    failed_stamp = float("inf")
                if not path.exists() or path.stat().st_mtime <= failed_stamp:
                    return ComponentHealth(ReadinessState.ERROR, failure.get("category", "fetch_error"))
            if not path.exists():
                return ComponentHealth(ReadinessState.MISSING, "Chưa có cache.")
            envelope = read_json(path)
            now = datetime.now(VIETNAM)
            item = self.by_symbol[symbol]
            if component == "quote":
                try:
                    quote_candle(envelope, symbol, item.exchange, self.calendar)
                except NoTradesYet:
                    return ComponentHealth(ReadinessState.READY, "Chưa khớp lệnh; dùng lịch sử đã đóng.")
                if (now - fetched_at(envelope)).total_seconds() > self.settings.stale_after_seconds:
                    return ComponentHealth(ReadinessState.STALE, "Bảng giá đã cũ.")
            else:
                rows = envelope["data"].get("data")
                expected_type = list if component == "ratios" else dict
                if not rows or not isinstance(rows, expected_type):
                    return ComponentHealth(ReadinessState.INVALID, "Bảng tài chính sai schema.")
                if (now - fetched_at(envelope)).days >= 7:
                    return ComponentHealth(ReadinessState.STALE, "Báo cáo tải đã cũ.")
                other = "income" if component == "ratios" else "ratios"
                other_path = self.directory / f"{symbol}-{other}.json"
                if other_path.exists() and f"{symbol}-{other}" not in failures:
                    ratios = envelope if component == "ratios" else read_json(other_path)
                    income = envelope if component == "income" else read_json(other_path)
                    parse_financials(ratios, income, symbol)
            return ComponentHealth(ReadinessState.READY)
        except (OSError, ValueError, KeyError, TypeError):
            return ComponentHealth(ReadinessState.INVALID, "Cache không vượt qua kiểm tra dữ liệu.")

    def ensure_components(self, symbol, components, timeout=4.0):
        components = tuple(dict.fromkeys(components))
        deadline = time.monotonic() + max(0, timeout)
        while True:
            health = {component: self.component_health(symbol, component) for component in components}
            if all(item.ready for item in health.values()):
                return health
            retryable = [component for component, item in health.items()
                         if item.state is not ReadinessState.FETCHING
                         and not self._failure_retry_pending(f"{symbol}-{component}")]
            if retryable:
                self.request_components(symbol, retryable, USER_PRIORITY)
                self.tick()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {component: self.component_health(symbol, component) for component in components}
            with self._condition:
                self._condition.wait(timeout=min(remaining, .25))

    @staticmethod
    def readiness_message(symbol, health):
        labels = {"quote": "bảng giá",
                  "ratios": "chỉ số tài chính", "income": "báo cáo kết quả kinh doanh"}
        pending = [labels.get(component, component) for component, item in health.items() if not item.ready]
        return (f"{symbol}: dữ liệu {', '.join(pending)} chưa sẵn sàng. "
                "Bot đang tự cập nhật từ Vietcap; vui lòng thử lại sau.")

    def describe(self):
        running = bool(self.thread and self.thread.is_alive())
        state = "đang cập nhật" if running else "đang chờ"
        if self.blocked:
            state = "đã dừng do nguồn từ chối; xem nhật ký và khởi động lại khi nguồn cho phép"
        detail = "chưa hoàn tất lượt đầu"
        if self.latest:
            counts = self.latest.get("status_counts", {})
            detail = (f"lượt cuối {self.latest.get('finished_at', '?')}; "
                      + ", ".join(f"{key} {counts.get(key, 0)}" for key in
                                  ("READY", "NO_TRADE", "MISSING", "INVALID", "ERROR")))
        with self._condition:
            queued = len(self.priority)
        return (f"Vietcap backup collector: {state}; {detail}.\n"
                f"Priority queue: {queued}.\n"
                "Bảng giá chỉ dùng để hiển thị; EMA/RSI chỉ dùng historical 1d đã đóng.")

    def quote_summary(self, symbol):
        """Return a display-only current quote; never feed it to the scanner."""
        if symbol not in self.by_symbol:
            return ""
        try:
            envelope = read_json(self.directory / f"{symbol}-quote.json")
            candle = quote_candle(envelope, symbol, self.by_symbol[symbol].exchange, self.calendar)
            observed = fetched_at(envelope)
            return (f"Bảng giá hiện tại (không dùng tính EMA/RSI): "
                    f"giá {candle.close:,.2f}, khối lượng {candle.volume:,}; "
                    f"cập nhật {observed:%d/%m/%Y %H:%M:%S %z}.")
        except NoTradesYet as waiting:
            return f"Bảng giá hiện tại: {waiting}"
        except (OSError, ValueError, KeyError, TypeError):
            return "Bảng giá hiện tại chưa sẵn sàng."

    def _financial_counts(self):
        counts = {name: 0 for name in ("READY", "MISSING", "STALE", "INVALID", "ERROR")}
        failures_path = self.directory / "failures.json"
        try:
            failures = read_json(failures_path) if failures_path.exists() else {}
        except (OSError, ValueError, KeyError, TypeError):
            failures = {}
        now = datetime.now(VIETNAM)
        for symbol in self.by_symbol:
            names = (f"{symbol}-ratios", f"{symbol}-income")
            paths = [self.directory / (name + ".json") for name in names]
            if any(name in failures for name in names):
                state = "ERROR"
            elif not all(path.exists() for path in paths):
                state = "MISSING"
            else:
                try:
                    ratios, income = (read_json(path) for path in paths)
                    parse_financials(ratios, income, symbol)
                    state = ("STALE" if (now - max(fetched_at(ratios), fetched_at(income))).days >= 7
                             else "READY")
                except (OSError, ValueError, KeyError, TypeError):
                    state = "INVALID"
            counts[state] += 1
        return counts

    def status_snapshot(self):
        with self._condition:
            queued = len(self.priority)
        latest = self.latest or {}
        return {
            "live_quote": latest.get("status_counts", {}),
            "financials": self._financial_counts(),
            "finished_at": latest.get("finished_at"),
            "provider_requests": latest.get("provider_requests", 0),
            "priority_queue": queued,
            "running": bool(self.thread and self.thread.is_alive()),
        }

    def tick(self):
        completed = None
        try:
            completed = self.results.get_nowait()
        except queue.Empty:
            pass
        if completed is not None:
            self.latest = completed
            self.blocked = bool(completed.get("blocked"))
            retry_delay = max(5, completed.get("source_retry_after", 0))
            self.next_run = max(self.started_monotonic + self.settings.scan_interval_seconds,
                                time.monotonic() + retry_delay)
        running = self.thread and self.thread.is_alive()
        now = datetime.now(VIETNAM)
        # Leave room to fetch confirmed closing data after the exchange closes.
        closing = self.calendar.is_trading_day(now.date()) and wall_time(15, 1) <= now.time() <= wall_time(15, 10)
        with self._condition:
            urgent = bool(self.priority)
        due = self.requested or self.calendar.is_scan_time(now) or closing
        if due and not running and not self.blocked and (urgent or time.monotonic() >= self.next_run):
            priorities = self._take_priority()
            self.requested = False
            self.started = now
            self.started_monotonic = time.monotonic()
            self.thread = threading.Thread(target=self._worker, args=(priorities,), daemon=True)
            self.thread.start()
        return completed

    def _worker(self, priorities):
        report = {"errors": {}}
        try:
            with ProcessLock(self.directory / ".collector.lock"):
                failures_path = self.directory / "failures.json"
                failures = read_json(failures_path) if failures_path.exists() else {}
                self._drain_priority(failures, priorities)
                report = refresh_quotes(self.directory, self.instruments, self.calendar, self.client, stop=self.stop)
                failures = read_json(failures_path) if failures_path.exists() else {}
                self._drain_priority(failures)
                report["component_errors"] = len(failures)
        except (OSError, ValueError, KeyError, TypeError) as error:
            report["errors"]["refresh"] = str(error)
            report["blocked"] = isinstance(error, SourceUnavailable) and getattr(error, "status", None) in {401, 403}
            report["source_retry_after"] = getattr(error, "retry_after", 0)
        finally:
            report["finished_at"] = datetime.now(VIETNAM).isoformat()
            self.results.put(report)

    def _drain_priority(self, failures, tasks=None):
        tasks = list(tasks if tasks is not None else self._take_priority())
        symbols = []
        for index, (symbol, component) in enumerate(tasks):
            if self.stop.is_set():
                break
            symbols.append(symbol)
            current = self.component_health(symbol, component, ignore_fetching=True)
            if current.ready:
                self._set_state(symbol, component, current)
                with self._condition:
                    self._inflight.discard((symbol, component))
                continue
            self._set_state(symbol, component, ComponentHealth(ReadinessState.FETCHING, "Đang tải."))
            try:
                self._component(symbol, component, failures)
            except SourceUnavailable:
                for pending_symbol, pending_component in tasks[index + 1:]:
                    with self._condition:
                        self._inflight.discard((pending_symbol, pending_component))
                    self.request_components(pending_symbol, (pending_component,), USER_PRIORITY)
                raise
        return symbols

    def _component(self, symbol, component, failures):
        name = f"{symbol}-{component}"
        path = self.directory / (name + ".json")
        try:
            if component == "quote":
                batch = self.client.quotes([symbol])
                rows = batch.get("data")
                if not isinstance(rows, list):
                    raise ValueError("Bảng giá không trả danh sách.")
                row = next((item for item in rows if item.get("listingInfo", {}).get("symbol") == symbol), None)
                if row is None:
                    raise ValueError("Bảng giá thiếu mã yêu cầu.")
                envelope = {**batch, "data": row}
                try:
                    quote_candle(envelope, symbol, self.by_symbol[symbol].exchange, self.calendar)
                except NoTradesYet:
                    pass
            else:
                envelope = self.client.financials(symbol, component)
                rows = envelope["data"].get("data")
                if not rows or not isinstance(rows, list if component == "ratios" else dict):
                    raise ValueError("Nguồn không trả bảng tài chính.")
                fetched_at(envelope)
            atomic_json(path, envelope)
            failures.pop(name, None)
            atomic_json(self.directory / "failures.json", failures)
            with self._condition:
                self._states.pop((symbol, component), None)
            self._set_state(symbol, component, self.component_health(symbol, component))
        except (OSError, ValueError, KeyError, TypeError) as error:
            _record_failure(failures, name, error)
            self._set_state(symbol, component, ComponentHealth(ReadinessState.ERROR, _error_category(error)))
            if isinstance(error, SourceUnavailable):
                raise
        finally:
            atomic_json(self.directory / "failures.json", failures)
            with self._condition:
                self._inflight.discard((symbol, component))
                self._condition.notify_all()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)
