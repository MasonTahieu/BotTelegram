"""Load local OHLCV/financial CSV files. No network access or implicit repair."""

import csv
import math
from datetime import date, datetime
from pathlib import Path

from fintech_bot.domain import Candle, FinancialSnapshot, VIETNAM


class CsvDataProvider:
    is_demo = False

    def __init__(self, candles_path: Path, financials_path: Path | None = None):
        self.path = candles_path
        self.financials_path = financials_path
        self.source = "local-csv:" + str(candles_path.resolve())
        self._stamp = None
        self._series = {}
        self._financial_stamp = None
        self._financials = {}
        self._financial_errors = {}

    def _reload(self):
        stamp = self.path.stat().st_mtime_ns
        if stamp == self._stamp:
            return
        series = {}
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"symbol", "timeframe", "closed_at", "open", "high", "low", "close", "volume", "is_closed"}
            if not required <= set(reader.fieldnames or []):
                raise ValueError("File giá thiếu cột: " + ", ".join(sorted(required - set(reader.fieldnames or []))))
            for row_number, row in enumerate(reader, 2):
                try:
                    end = datetime.fromisoformat(row["closed_at"])
                    if end.utcoffset() is None:
                        raise ValueError("closed_at phải có múi giờ")
                    end = end.astimezone(VIETNAM)
                    flag = row["is_closed"].strip().lower()
                    if flag not in {"true", "false"}:
                        raise ValueError("is_closed phải là true hoặc false")
                    observed = datetime.fromisoformat(row["observed_at"]) if row.get("observed_at") else end
                    if observed.utcoffset() is None:
                        raise ValueError("observed_at phải có múi giờ")
                    symbol = row["symbol"].strip().upper()
                    timeframe = row["timeframe"].strip()
                    if timeframe != "1d":
                        raise ValueError("bot chỉ nhận dữ liệu 1d")
                    candle = Candle(symbol, end.date(), float(row["open"]), float(row["high"]),
                                    float(row["low"]), float(row["close"]), int(row["volume"]),
                                    end, timeframe, flag == "true", observed.astimezone(VIETNAM))
                    series.setdefault((symbol, timeframe), []).append(candle)
                except (ValueError, TypeError) as error:
                    raise ValueError(f"File giá dòng {row_number}: {error}") from error
        self._series, self._stamp = series, stamp

    def get_candles(self, symbol, timeframe="1d"):
        self._reload()
        return list(self._series.get((symbol, timeframe), []))

    def get_financials(self, symbol):
        if self.financials_path is None:
            return None
        stamp = self.financials_path.stat().st_mtime_ns
        if stamp == self._financial_stamp:
            if symbol in self._financial_errors:
                raise ValueError(self._financial_errors[symbol])
            return self._financials.get(symbol)
        fields = ("eps", "pe", "pb", "roe", "debt_to_equity", "revenue_growth", "profit_growth")
        snapshots, errors = {}, {}
        with self.financials_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"symbol", "period", "published_on"} <= set(reader.fieldnames or []):
                raise ValueError("File tài chính thiếu cột symbol, period hoặc published_on.")
            for number, row in enumerate(reader, 2):
                ticker = (row.get("symbol") or "").strip().upper()
                if not ticker:
                    raise ValueError(f"File tài chính dòng {number}: thiếu mã cổ phiếu.")
                try:
                    values = {name: float(row[name]) if row.get(name) else None for name in fields}
                    if any(value is not None and not math.isfinite(value) for value in values.values()):
                        raise ValueError("Chỉ tiêu tài chính phải là số hữu hạn")
                    snapshot = FinancialSnapshot(ticker, row["period"], date.fromisoformat(row["published_on"]), **values)
                    if ticker not in snapshots or snapshot.published_on > snapshots[ticker].published_on:
                        snapshots[ticker] = snapshot
                except (ValueError, TypeError) as error:
                    errors[ticker] = f"File tài chính dòng {number}: {error}"
        self._financials, self._financial_errors, self._financial_stamp = snapshots, errors, stamp
        if symbol in errors:
            raise ValueError(errors[symbol])
        return snapshots.get(symbol)
