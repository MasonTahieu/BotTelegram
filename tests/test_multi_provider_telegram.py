import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fintech_bot.app import build_application
from fintech_bot.config import Settings, StrategySettings
from fintech_bot.domain import Signal, SignalSide, VIETNAM
from fintech_bot.market import ReplayClock
from fintech_bot.services.alerts import AlertService
from fintech_bot.storage.sqlite import SqliteRepository
from tests.test_project import RecordingNotifier


NOW = datetime(2026, 9, 18, 15, tzinfo=VIETNAM)


def signal(symbol="FPT", side=SignalSide.BUY, source="VNSTOCK_KBS", confirmed=True):
    return Signal(symbol, NOW.date(), side, "strategy", "cross", {"rsi": 55}, source, True,
                  NOW, "1d", confirmed, "HOSE", None if confirmed else NOW.replace(hour=10))


class MultiProviderTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_signals_today_reads_mixed_sources_and_dedupes_provider_switch(self):
        settings = Settings(("FPT", "HPG", "VNM"), self.root / "app.db", StrategySettings())
        app = build_application(settings, RecordingNotifier(), clock=ReplayClock(NOW))
        self.addCleanup(app.close)
        strategy = app.scanner.strategy.settings.strategy_id
        fpt = replace(signal(), strategy_id=strategy)
        mbb = replace(signal("HPG", SignalSide.SELL, "VIETCAP_DIRECT"), strategy_id=strategy)
        app.repository.save_signal(fpt)
        app.repository.save_signal(replace(fpt, source="VIETCAP_DIRECT", reason="same logical event"))
        app.repository.save_signal(mbb)
        with patch.object(app.scanner, "refresh_if_needed", return_value=None):
            output = app.commands.handle("/signals today", "alice")
        self.assertIn("FPT: MUA", output)
        self.assertIn("HPG: BÁN", output)
        self.assertEqual(output.count("FPT: MUA"), 1)

    def test_subscription_fallback_duplicate_suppression_and_restart_persistence(self):
        path = self.root / "alerts.db"
        settings = Settings(("FPT",), path, StrategySettings(), scan_interval_seconds=900)
        clock = ReplayClock(NOW)
        notifier = RecordingNotifier()
        repo = SqliteRepository(path)
        alerts = AlertService(repo, notifier, settings, clock)
        alerts.register("alice")
        repo.subscribe("alice", "FPT")
        first = signal(source="VNSTOCK_KBS")
        repo.save_signal(first)
        alerts.dispatch([first])
        fallback = replace(first, source="VIETCAP_DIRECT", reason="fallback")
        repo.save_signal(fallback)
        alerts.dispatch([fallback])
        self.assertEqual(len(notifier.messages), 1)
        repo.close()

        restarted_repo = SqliteRepository(path)
        self.addCleanup(restarted_repo.close)
        restarted = AlertService(restarted_repo, notifier, settings, clock)
        self.assertEqual(restarted_repo.subscriptions("alice"), ["FPT"])
        restarted_repo.save_signal(fallback)
        restarted.dispatch([fallback])
        self.assertEqual(len(notifier.messages), 1)

    def test_preview_is_ignored_and_confirmed_signal_reaches_both_users(self):
        repo = SqliteRepository(self.root / "multi.db")
        self.addCleanup(repo.close)
        settings = Settings(("FPT",), self.root / "unused.db", StrategySettings())
        notifier = RecordingNotifier()
        alert_clock = ReplayClock(NOW.replace(hour=14, minute=55))
        alerts = AlertService(repo, notifier, settings, alert_clock)
        for user in ("alice", "bob"):
            alerts.register(user)
        repo.subscribe("alice", "FPT")
        repo.subscribe("bob", "FPT")
        early = replace(signal(confirmed=False), observed_at=alert_clock.now())
        repo.save_signal(early)
        alerts.dispatch([early])
        repo.save_signal(replace(early, source="VIETCAP_DIRECT"))
        alerts.dispatch([replace(early, source="VIETCAP_DIRECT")])
        confirmed = signal(confirmed=True)
        repo.save_signal(confirmed)
        alerts.dispatch([confirmed])
        self.assertEqual([chat for chat, _ in notifier.messages], ["alice", "bob"])


if __name__ == "__main__":
    unittest.main()
