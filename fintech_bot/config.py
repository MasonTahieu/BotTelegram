"""TOML settings, validated before starting the application."""

import math
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.toml"


@dataclass(frozen=True)
class StrategySettings:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    buy_rsi_min: float = 50.0
    sell_rsi_level: float = 70.0

    def __post_init__(self) -> None:
        periods = (self.ema_fast, self.ema_slow, self.rsi_period)
        if any(type(value) is not int or value < 2 for value in periods):
            raise ValueError("Chu kỳ chỉ báo phải là số nguyên từ 2 trở lên.")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("EMA nhanh phải có chu kỳ nhỏ hơn EMA chậm.")
        for value in (self.buy_rsi_min, self.sell_rsi_level):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value < 100):
                raise ValueError("Ngưỡng RSI phải nằm giữa 0 và 100.")

    @property
    def required_bars(self) -> int:
        return max(self.ema_slow + 1, self.rsi_period + 2)

    @property
    def strategy_id(self) -> str:
        return (
            f"ema-rsi-v1:{self.ema_fast}:{self.ema_slow}:{self.rsi_period}:"
            f"{self.buy_rsi_min:g}:{self.sell_rsi_level:g}"
        )


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    database_path: Path
    strategy: StrategySettings
    mode: str = "demo"
    timeframe: str = "1d"
    exchanges: tuple[str, ...] = ("HOSE", "HNX", "UPCOM")
    universe_path: Path | None = None
    candles_path: Path | None = None
    financials_path: Path | None = None
    holidays: tuple[date, ...] = ()
    scan_interval_seconds: int = 900
    cache_seconds: int = 30
    stale_after_seconds: int = 1200
    alert_mode: str = "watchlist"
    max_alerts_per_cycle: int = 10
    retry_seconds: int = 5
    demo_start: str = "2026-09-18T10:00:00+07:00"
    vietcap_path: Path | None = None
    vnstock_path: Path | None = None
    primary_provider: str = "vietcap"
    primary_market_source: str = "KBS"
    primary_financial_source: str = "KBS"
    backup_provider: str = "vietcap"
    enable_failover: bool = False

    def __post_init__(self) -> None:
        if type(self.enable_failover) is not bool:
            raise ValueError("data.enable_failover phải là true hoặc false.")
        # Backward-compatible migration for existing config files. Runtime is
        # daily-only; accepting the old value here prevents an upgrade from
        # breaking subscriptions or refusing to start.
        if self.timeframe == "5m":
            object.__setattr__(self, "timeframe", "1d")
        elif self.timeframe != "1d":
            raise ValueError("Bot chỉ hỗ trợ khung nến 1d.")
        if self.mode not in {"demo", "csv", "vietcap"}:
            raise ValueError("Chế độ hợp lệ: demo, csv hoặc vietcap.")
        if self.mode == "vietcap" and self.vietcap_path is None:
            raise ValueError("Chế độ vietcap cần data.vietcap_path.")
        if self.primary_provider not in {"vietcap", "vnstock"} or self.backup_provider != "vietcap":
            raise ValueError("Nguồn chính hợp lệ: vietcap/vnstock; nguồn dự phòng phải là vietcap.")
        if self.primary_provider == "vnstock" and self.vnstock_path is None:
            raise ValueError("Nguồn chính vnstock cần data.vnstock_path.")
        if self.primary_market_source.upper() != "KBS" or self.primary_financial_source.upper() != "KBS":
            raise ValueError("Bản migration này chỉ hỗ trợ Vnstock/KBS; không tự chuyển sang VCI.")
        if not self.exchanges or set(self.exchanges) - {"HOSE", "HNX", "UPCOM"}:
            raise ValueError("Sàn hợp lệ: HOSE, HNX, UPCOM.")
        if self.alert_mode not in {"watchlist", "all", "off"}:
            raise ValueError("Chế độ thông báo phải là watchlist, all hoặc off.")
        for value in (self.scan_interval_seconds, self.cache_seconds, self.stale_after_seconds,
                      self.max_alerts_per_cycle, self.retry_seconds):
            if type(value) is not int or value < 1:
                raise ValueError("Thời gian và giới hạn thông báo phải là số nguyên dương.")
        if self.stale_after_seconds < self.scan_interval_seconds:
            raise ValueError("Ngưỡng stale không được nhỏ hơn chu kỳ quét.")
        if datetime.fromisoformat(self.demo_start).utcoffset() is None:
            raise ValueError("demo_start cần có múi giờ, ví dụ +07:00.")
        if self.mode == "csv" and (self.universe_path is None or self.candles_path is None):
            raise ValueError("Chế độ csv cần universe_path và candles_path.")


def load_settings(path: Path = DEFAULT_CONFIG) -> Settings:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    for section in ("data", "runtime", "alerts", "strategy"):
        if not isinstance(raw.get(section, {}), dict):
            raise ValueError(f"Cấu hình {section} phải là một bảng [{section}].")
    symbols = raw.get("symbols", [])
    if not isinstance(symbols, list):
        raise ValueError("symbols phải là danh sách mã.")
    if any(not isinstance(item, str) or not re.fullmatch(r"[A-Z0-9]{2,10}", item)
           for item in symbols):
        raise ValueError("Mã cổ phiếu phải gồm 2–10 chữ in hoa hoặc chữ số.")
    if len(set(symbols)) != len(symbols):
        raise ValueError("Danh sách symbols không được trùng mã.")
    db_value = raw.get("database_path", "data/demo.sqlite3")
    if not isinstance(db_value, str) or not db_value.strip():
        raise ValueError("database_path phải là đường dẫn không rỗng.")
    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    runtime = raw.get("runtime", {})
    data = raw.get("data", {})
    alerts = raw.get("alerts", {})
    exchanges = data.get("exchanges", ["HOSE", "HNX", "UPCOM"])
    if not isinstance(exchanges, list) or any(not isinstance(item, str) for item in exchanges):
        raise ValueError("data.exchanges phải là danh sách tên sàn.")
    holidays = data.get("holidays", [])
    if not isinstance(holidays, list):
        raise ValueError("data.holidays phải là danh sách ngày nghỉ.")
    def local_path(name):
        value = data.get(name)
        if not value:
            return None
        candidate = Path(value)
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
    universe_path = local_path("universe_path")
    if not symbols and universe_path is None and raw.get("mode") != "vietcap":
        raise ValueError("Cần symbols hoặc file danh sách mã universe_path.")
    return Settings(
        tuple(symbols), db_path, StrategySettings(**raw.get("strategy", {})),
        mode=raw.get("mode", "demo"), timeframe=raw.get("timeframe", "1d"),
        exchanges=tuple(exchanges),
        universe_path=universe_path, candles_path=local_path("candles_path"),
        financials_path=local_path("financials_path"),
        holidays=tuple(date.fromisoformat(str(item)) for item in holidays),
        scan_interval_seconds=runtime.get("scan_interval_seconds", 900),
        cache_seconds=runtime.get("cache_seconds", 30),
        stale_after_seconds=runtime.get("stale_after_seconds", 1200),
        alert_mode=alerts.get("default_mode", "watchlist"),
        max_alerts_per_cycle=alerts.get("max_per_cycle", 10),
        retry_seconds=alerts.get("retry_seconds", 5),
        demo_start=runtime.get("demo_start", "2026-09-18T10:00:00+07:00"),
        vietcap_path=local_path("vietcap_path"),
        vnstock_path=local_path("vnstock_path"),
        primary_provider=str(data.get("primary_provider", "vietcap")).lower(),
        primary_market_source=str(data.get("primary_market_source", "KBS")).upper(),
        primary_financial_source=str(data.get("primary_financial_source", "KBS")).upper(),
        backup_provider=str(data.get("backup_provider", "vietcap")).lower(),
        enable_failover=data.get("enable_failover", False),
    )
