import csv
import re
from pathlib import Path

from fintech_bot.domain import Instrument


def load_universe(path: Path) -> list[Instrument]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"symbol", "exchange", "name", "asset_type", "status"} <= set(reader.fieldnames or []):
            raise ValueError("Danh sách mã thiếu cột symbol, exchange, name, asset_type hoặc status.")
        result = []
        seen = set()
        for number, row in enumerate(reader, 2):
            symbol = row["symbol"].strip().upper()
            exchange = row["exchange"].strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{2,10}", symbol) or symbol in seen:
                raise ValueError(f"Danh sách mã dòng {number}: mã không hợp lệ hoặc bị trùng.")
            if exchange not in {"HOSE", "HNX", "UPCOM"}:
                raise ValueError(f"Danh sách mã dòng {number}: sàn không hợp lệ.")
            status = row["status"].strip().lower()
            if status not in {"active", "halted", "delisted"}:
                raise ValueError(f"Danh sách mã dòng {number}: status không hợp lệ.")
            result.append(Instrument(symbol, exchange, row["name"].strip(), row["asset_type"].strip().lower(), status))
            seen.add(symbol)
    if not result:
        raise ValueError("Danh sách mã rỗng.")
    return result


def select_universe(instruments, exchanges, symbols=()):
    return [item for item in instruments if item.exchange in exchanges and item.asset_type == "stock"
            and (not symbols or item.symbol in symbols)]
