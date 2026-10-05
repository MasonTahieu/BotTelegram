"""Composition root: replace providers and transports here when APIs are ready."""

from dataclasses import dataclass
from datetime import datetime

from fintech_bot.bot.commands import CommandRouter
from fintech_bot.config import Settings
from fintech_bot.data.base import MarketDataProvider
from fintech_bot.data.replay import ReplayDataProvider
from fintech_bot.data.csv_provider import CsvDataProvider
from fintech_bot.data.vietcap import VietcapCacheProvider
from fintech_bot.data.provider_manager import DataProviderManager
from fintech_bot.data.vnstock_provider import VnstockProvider
from fintech_bot.data.universe import load_universe, select_universe
from fintech_bot.domain import Instrument
from fintech_bot.market import Clock, ReplayClock, MarketCalendar
from fintech_bot.services.alerts import AlertService, ConsoleNotifier, Notifier
from fintech_bot.services.scanner import Scanner
from fintech_bot.services.scheduler import Scheduler
from fintech_bot.services.financial_filter import FinancialFilter
from fintech_bot.storage.sqlite import SqliteRepository
from fintech_bot.strategies.ema_rsi import EmaRsiStrategy


@dataclass
class Application:
    repository: SqliteRepository
    scanner: Scanner
    alerts: AlertService
    commands: CommandRouter
    scheduler: Scheduler
    clock: Clock

    def close(self) -> None:
        self.repository.close()


def build_application(settings: Settings, notifier: Notifier | None = None, clock=None,
                      *, provider: MarketDataProvider | None = None) -> Application:
    calendar = MarketCalendar(settings.holidays)
    if clock is None:
        clock = (ReplayClock(datetime.fromisoformat(settings.demo_start))
                 if provider is None and settings.mode == "demo" else Clock())
    if provider is None and settings.mode == "vietcap":
        vietcap = VietcapCacheProvider(settings.vietcap_path, calendar)
        if settings.primary_provider == "vnstock":
            vnstock = VnstockProvider(
                settings.vnstock_path, calendar, instruments=vietcap.instruments,
                required_bars=settings.strategy.required_bars,
                stale_after_seconds=settings.stale_after_seconds, clock=clock,
                periodic_refresh_seconds=settings.scan_interval_seconds,
            )
            vnstock.ensure_universe()
            provider = DataProviderManager(
                vnstock, vietcap, calendar, [*vietcap.instruments, *vnstock.instruments],
                required_bars=settings.strategy.required_bars,
                stale_after_seconds=settings.stale_after_seconds, clock=clock,
                enable_failover=settings.enable_failover,
                cache_directory=settings.vnstock_path.parent / "daily",
            )
        else:
            provider = vietcap
    instruments = (provider.instruments if settings.mode == "vietcap" and hasattr(provider, "instruments") else
                   load_universe(settings.universe_path) if settings.universe_path else
                   [Instrument(symbol, "HOSE", "Nhãn mẫu") for symbol in settings.symbols])
    instruments = select_universe(instruments, settings.exchanges, settings.symbols)
    if not instruments:
        raise ValueError("Không có cổ phiếu nào khớp phạm vi cấu hình.")
    if provider is None:
        provider = (ReplayDataProvider(instruments, clock, calendar) if settings.mode == "demo" else
                    CsvDataProvider(settings.candles_path, settings.financials_path))
    repository = SqliteRepository(settings.database_path)
    repository.save_instruments(instruments)
    strategy = EmaRsiStrategy(settings.strategy)
    scanner = Scanner(provider, strategy, repository, instruments, settings, clock, calendar)
    alerts = AlertService(repository, notifier if notifier is not None else ConsoleNotifier(), settings, clock)
    alerts.financial_filter = FinancialFilter(repository, provider, clock)
    commands = CommandRouter(scanner, repository, alerts)
    scheduler = Scheduler(scanner, alerts, clock, calendar, settings.scan_interval_seconds)
    return Application(repository, scanner, alerts, commands, scheduler, clock)
