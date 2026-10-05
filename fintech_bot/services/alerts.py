import logging
from dataclasses import dataclass, field
from typing import Protocol

from fintech_bot.bot.formatting import format_signal
from fintech_bot.domain import SignalSide

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, chat_id: str, text: str) -> None: ...


class ConsoleNotifier:
    def send(self, chat_id: str, text: str) -> None:
        print(f"\n--- THÔNG BÁO LOCAL cho {chat_id} ---\n{text}\n")


@dataclass
class AlertReport:
    sent: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)


class AlertService:
    def __init__(self, repository, notifier, settings, clock):
        self.repository, self.notifier = repository, notifier
        self.settings, self.clock = settings, clock
        self.window_start = None
        self.window_attempts = 0
        self.financial_filter = None

    def financially_eligible(self, chat_id, signal):
        # Fundamental investment filters gate entries, never risk-reducing exits.
        return (signal.side != SignalSide.BUY or self.financial_filter is None
                or self.financial_filter.check(chat_id, signal.symbol)[0])

    def eligible(self, chat_id, signal):
        return signal.confirmed and self.financially_eligible(chat_id, signal)

    def register(self, chat_id):
        mode, timeframe = self.repository.preference(chat_id, self.settings.alert_mode, self.settings.timeframe)
        self.repository.set_preference(chat_id, mode, timeframe)

    def dispatch(self, signals):
        now = self.clock.now().timestamp()
        for signal in signals:
            self.repository.cancel_superseded(signal)
            if signal.side == SignalSide.NONE:
                continue
            if not signal.is_demo and signal.session != self.clock.now().date():
                continue
            ttl = 86400
            origin = signal.closed_at
            expires_at = min(now + ttl, origin.timestamp() + ttl) if origin else now + ttl
            if expires_at < now:
                continue
            for chat_id in self.repository.recipients(signal.symbol, signal.timeframe):
                if self.eligible(chat_id, signal):
                    self.repository.enqueue(signal, chat_id, now, expires_at)
        return self.flush()

    def flush(self):
        report = AlertReport()
        now = self.clock.now().timestamp()
        if self.window_start is None or now - self.window_start >= self.settings.scan_interval_seconds or now < self.window_start:
            self.window_start, self.window_attempts = now, 0
        budget = max(0, self.settings.max_alerts_per_cycle - self.window_attempts)
        for row in self.repository.pending(now, budget):
            signal = self.repository.signal_by_key(row["event_key"])
            event_key = row["event_key"]
            chat_id = row["chat_id"]
            if (chat_id not in self.repository.recipients(signal.symbol, signal.timeframe)
                    or not self.eligible(chat_id, signal)
                    or (not signal.is_demo and signal.session != self.clock.now().date())):
                self.repository.update_outbox(event_key, chat_id, "cancelled")
                continue
            try:
                self.window_attempts += 1
                self.notifier.send(chat_id, format_signal(signal))
                self.repository.mark_delivered(event_key, chat_id)
                self.repository.update_outbox(event_key, chat_id, "sent")
                report.sent += 1
            except Exception as error:
                logger.error("Notification failed for %s / %s: %s", signal.symbol, chat_id, error)
                delay = min(300, self.settings.retry_seconds * (2 ** row["attempts"]))
                delay = max(delay, getattr(error, "retry_after", 0))
                status = "failed" if row["attempts"] >= 7 or getattr(error, "permanent", False) else "pending"
                self.repository.update_outbox(event_key, chat_id, status, now + delay, str(error))
                report.errors.append(f"Không gửi được {signal.symbol} tới {chat_id}; sẽ thử lại nếu còn hiệu lực.")
        report.pending = self.repository.pending_count()
        return report
