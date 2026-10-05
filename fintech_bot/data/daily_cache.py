"""Normalized, provider-neutral daily history cache."""

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from fintech_bot.data.readiness import DataNotReady, InvalidData
from fintech_bot.data.validation import validate_candles
from fintech_bot.data.vietcap_http import atomic_json, read_json
from fintech_bot.domain import Candle, VIETNAM


class LocalDailyCache:
    source = "LOCAL_DAILY_CACHE"

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol):
        return self.directory / f"{symbol}-daily.json"

    @staticmethod
    def _payload(candle):
        value = asdict(candle)
        value["session"] = candle.session.isoformat()
        value["closed_at"] = candle.closed_at.isoformat() if candle.closed_at else None
        value["observed_at"] = candle.observed_at.isoformat() if candle.observed_at else None
        return value

    @staticmethod
    def _candle(value):
        value = dict(value)
        value["session"] = date.fromisoformat(value["session"])
        for name in ("closed_at", "observed_at"):
            value[name] = datetime.fromisoformat(value[name]) if value.get(name) else None
        return Candle(**value)

    def read(self, symbol):
        path = self._path(symbol)
        if not path.exists():
            raise DataNotReady("Chưa có historical daily cache hợp nhất.")
        try:
            envelope = read_json(path)
            if (envelope.get("schema") != 1 or envelope.get("symbol") != symbol
                    or envelope.get("timeframe") != "1d"):
                raise ValueError("Sai schema/mã/khung.")
            fetched = datetime.fromisoformat(envelope["fetched_at"])
            if fetched.utcoffset() is None:
                raise ValueError("Thời gian cache thiếu múi giờ.")
            source = envelope.get("data_source", self.source)
            if source not in {"VNSTOCK_KBS", "VIETCAP_DIRECT"}:
                # Older builds wrote values such as
                # VNSTOCK_KBS+VIETCAP_LIVE back into history. Preserve the file
                # on disk, but force a rebuild from one complete source.
                raise ValueError("Cache cũ đã trộn provenance và cần được dựng lại.")
            candles = [self._candle(item) for item in envelope["candles"]]
            validate_candles(symbol, candles, 1)
            if any(not item.is_closed or item.timeframe != "1d" for item in candles):
                raise ValueError("Canonical daily cache chỉ được chứa nến ngày đã đóng.")
            return candles, fetched.astimezone(VIETNAM), source
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InvalidData("Historical daily cache không hợp lệ.") from error

    def write(self, symbol, candles, data_source, fetched_at=None):
        validate_candles(symbol, candles, 1)
        if data_source not in {"VNSTOCK_KBS", "VIETCAP_DIRECT"}:
            raise ValueError("Nguồn historical cache không hợp lệ.")
        if any(not item.is_closed or item.timeframe != "1d" for item in candles):
            raise ValueError("Chỉ lưu nến ngày đã đóng vào historical cache.")
        observed = fetched_at or datetime.now(VIETNAM)
        if observed.utcoffset() is None:
            raise ValueError("Thời gian cache thiếu múi giờ.")
        atomic_json(self._path(symbol), {
            "schema": 1,
            "symbol": symbol,
            "timeframe": "1d",
            "data_source": data_source,
            "fetched_at": observed.astimezone(VIETNAM).isoformat(),
            "candles": [self._payload(item) for item in candles[-1000:]],
        })
