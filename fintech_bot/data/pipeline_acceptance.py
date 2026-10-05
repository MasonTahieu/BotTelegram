"""Concise layered acceptance audit for the daily pipeline."""

import argparse
import time
from collections import Counter
from pathlib import Path

from fintech_bot.app import build_application
from fintech_bot.config import PROJECT_ROOT, load_settings
from fintech_bot.data.vietcap_http import VietcapClient, atomic_json
from fintech_bot.services.live_data import refresh_quotes


def _interleave(instruments):
    groups = {exchange: iter([item for item in instruments if item.exchange == exchange])
              for exchange in ("HOSE", "HNX", "UPCOM")}
    result = []
    while groups:
        for exchange in list(groups):
            item = next(groups[exchange], None)
            if item is None:
                del groups[exchange]
            else:
                result.append(item)
    return result


def run(settings, limit=None, refresh=True):
    app = build_application(settings)
    try:
        selected_items = _interleave(list(app.scanner.instruments.values()))
        if limit:
            selected_items = selected_items[:limit]
        symbols = [item.symbol for item in selected_items]
        client = VietcapClient(interval=1, timeout=10)
        quote_report = (refresh_quotes(settings.vietcap_path, selected_items,
                                       app.scanner.calendar, client) if refresh else None)
        primary = getattr(app.scanner.provider, "primary", None)
        kbs_before = getattr(primary, "api_call_count", 0)
        started = time.monotonic()
        app.scanner.begin_cycle(symbols)
        report = app.scanner.run(symbols)
        snapshot = app.scanner.finish_cycle() or {}
        duration = time.monotonic() - started
        kbs_after = getattr(primary, "api_call_count", 0)
        result = {
            "selected": len(symbols),
            "exchanges": dict(Counter(item.exchange for item in selected_items)),
            "historical": snapshot.get("historical", {}),
            "live_quote": snapshot.get("live_quote", {}),
            "scanner": snapshot.get("scanner", {}),
            "financials": snapshot.get("financials", {}),
            "failure_groups": snapshot.get("failure_groups", {}),
            "scan_seconds": duration,
            "signals": len(report.signals),
            "scan_errors": len(report.errors),
            "provider_requests": {
                "kbs_during_scan": max(0, kbs_after - kbs_before),
                "vietcap_quote": (quote_report or {}).get("provider_requests", 0),
            },
        }
        atomic_json(settings.vnstock_path / "pipeline-acceptance-last.json", result)
        return result
    finally:
        app.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit daily historical/quote/scanner/financial layers")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "vietcap.toml")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit phải dương")
    result = run(load_settings(args.config), args.limit, not args.no_refresh)
    print(result)
    return 0 if not result["scan_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
