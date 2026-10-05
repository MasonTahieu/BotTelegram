"""Reproducible local load check: python -m fintech_bot.benchmark --symbols 1500."""

import argparse
import json
import tempfile
from pathlib import Path
from time import perf_counter

from fintech_bot.app import build_application
from fintech_bot.config import Settings, StrategySettings


def main():
    parser = argparse.ArgumentParser(description="Benchmark synthetic data; no API or Telegram calls")
    parser.add_argument("--symbols", type=int, default=1500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.symbols <= 10000:
        parser.error("--symbols must be between 1 and 10000")
    with tempfile.TemporaryDirectory(prefix="fintech-benchmark-") as directory:
        root = Path(directory)
        universe = root / "universe.csv"
        universe.write_text("symbol,exchange,name,asset_type,status\n" + "".join(
            f"T{i:04d},{('HOSE', 'HNX', 'UPCOM')[i % 3]},Synthetic,stock,active\n"
            for i in range(args.symbols)), encoding="utf-8")
        start = perf_counter()
        app = build_application(Settings((), root / "state.sqlite3", StrategySettings(), universe_path=universe))
        result = {"symbols": args.symbols, "is_demo": True, "includes_network": False,
                  "init_seconds": round(perf_counter() - start, 3)}
        failed = False
        try:
            for label in ("first", "unchanged", "next_bar"):
                if label == "next_bar":
                    app.clock.advance(300)
                changes = app.repository.connection.total_changes
                start = perf_counter()
                report = app.scanner.run()
                failed = failed or bool(report.errors) or len(report.signals) != args.symbols * 2
                result[label] = {
                    "seconds": round(perf_counter() - start, 3), "results": len(report.signals),
                    "errors": len(report.errors), "rows_changed": app.repository.connection.total_changes - changes,
                }
        finally:
            app.close()
    payload = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
