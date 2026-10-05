"""Call tick from the main loop; all SQLite work stays on that same thread."""

from dataclasses import dataclass


@dataclass
class TickResult:
    scanned: bool = False
    sent: int = 0
    errors: int = 0
    reason: str = ""


class Scheduler:
    def __init__(self, scanner, alerts, clock, calendar, interval_seconds):
        self.scanner, self.alerts, self.clock, self.calendar = scanner, alerts, clock, calendar
        self.interval = interval_seconds
        self.next_scan = None

    def tick(self):
        now = self.clock.now()
        if not self.calendar.is_scan_time(now):
            return TickResult(reason="Ngoài phiên, cuối tuần hoặc ngày nghỉ cấu hình.")
        if self.next_scan is not None and now.timestamp() < self.next_scan:
            alerts = self.alerts.flush()
            return TickResult(sent=alerts.sent, errors=len(alerts.errors), reason="Đang chờ lượt quét kế tiếp.")
        report = self.scanner.run()
        alerts = self.alerts.dispatch(report.signals)
        # A slow source must not trigger immediate back-to-back polling.
        self.next_scan = self.clock.now().timestamp() + self.interval
        return TickResult(True, alerts.sent, len(report.errors) + len(alerts.errors), "Đã quét.")
