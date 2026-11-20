"""Calibrate, replay, collect or run the Week 05 strategy."""

import argparse
import asyncio
import csv
import json
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from copper.replay import replay
from copper.results import export, export_sensitivity, export_spreads, write_csv
from copper.strategy import (BASE_FIRST, BASE_LAST, ENTRY_OPEN, FORCE_CLOSE, KST, STEP, TEST_FIRST, TEST_LAST,
                             Costs, Thresholds, calibrate)

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
MONTHS = ("HGH27",)
COSTS = Costs(0, 1)
RAW_FIELDS = ("start", "open", "high", "low", "close", "volume")


def nh_options(parser, *, stamps=True):
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--exchange", default="FCME")
    if stamps:
        parser.add_argument("--bar-stamp", choices=("start", "end"), required=True)


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", type=Path, default=RESULTS)
    sub = ap.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Download confirmed REST bars and inspect coverage")
    nh_options(collect)
    collect.add_argument("--symbols", nargs="+", default=MONTHS)
    collect.add_argument("--first", type=date.fromisoformat, default=BASE_FIRST)
    collect.add_argument("--last", type=date.fromisoformat, default=BASE_LAST)
    inspect = sub.add_parser("inspect", help="Check NH symbol mappings and sample raw chart timestamps")
    nh_options(inspect, stamps=False)
    inspect.add_argument("--date", type=date.fromisoformat, required=True)
    inspect.add_argument("--symbols", nargs="+", default=MONTHS)
    quotes = sub.add_parser("quotes", help="Sample both monthly contracts via WebSocket")
    nh_options(quotes, stamps=False)
    quotes.add_argument("--seconds", type=int, required=True)
    quotes.add_argument("--symbols", nargs="+", default=MONTHS)
    calibration = sub.add_parser("calibrate", help="Freeze thresholds using only the baseline period")
    calibration.add_argument("--symbol", required=True, choices=("HGH27", "MHGH27", "MHGZ26"))
    calibration.add_argument("--selection-reason", required=True)
    historical = sub.add_parser("replay", help="Compare A/B/C/C-1 and run slippage sensitivity")
    historical.add_argument("--exclude-date", type=date.fromisoformat, action="append", default=[])
    nh_options(sub.add_parser("paper", help="Trade strategy C in the NH paper account"))
    return ap


def load_thresholds(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (raw["baseline_first"], raw["baseline_last"]) != (str(BASE_FIRST), str(BASE_LAST)):
        raise ValueError("Unexpected threshold calibration period")
    return Thresholds(**raw["thresholds"])


def coverage(symbol, bars, first, last, now):
    expected = set()
    day = first
    while day <= min(last, now.date()):
        if day.weekday() < 5:
            start = datetime.combine(day, ENTRY_OPEN, KST)
            end = min(datetime.combine(day, FORCE_CLOSE, KST), now)
            while start + STEP <= end:
                expected.add(start)
                start += STEP
        day += timedelta(days=1)
    found = [bar for bar in bars if bar.start in expected]
    zero = sum(bar.volume == 0 for bar in found)
    return dict(symbol=symbol, expected=len(expected), received=len(found), volume=sum(b.volume for b in found),
                zero_volume_ratio=zero / len(found) if found else None,
                missing_ratio=(len(expected) - len(found)) / len(expected) if expected else None)


async def collect_quotes(root, session, exchange, symbols, seconds):
    from copper.live import Feed, Journal, quote_symbol

    if seconds < 1:
        raise ValueError("Quote collection duration must be positive")
    codes = {quote_symbol(symbol, exchange): symbol for symbol in symbols}
    root.mkdir(parents=True, exist_ok=True)
    path = root / "quotes.csv"
    with path.open("x", encoding="utf-8", newline="", buffering=1) as handle:
        writer = csv.writer(handle)
        writer.writerow(("at", "symbol", "bid", "ask", "spread_ticks", "channel"))
        stop = asyncio.Event()
        feed = Feed(codes, session, writer, Journal("MHG", root=root))
        task = asyncio.create_task(feed.stream.run(stop))
        try:
            await asyncio.wait({task}, timeout=seconds)
        finally:
            stop.set()
            await task
    export_spreads(root, path)


def main():
    ap = parser()
    args = ap.parse_args()
    root = args.results
    threshold_path = root / "thresholds.json"
    if args.command in ("calibrate", "replay"):
        from copper.market import load_bars

        if args.command == "calibrate":
            if threshold_path.exists():
                ap.error(f"Thresholds already frozen in {threshold_path}")
            execution_symbol = args.symbol
            bars = load_bars(root / "raw" / f"{args.symbol}.csv")
            thresholds = calibrate(bars, COSTS, symbol=args.symbol, execution_symbol=execution_symbol)
            document = dict(baseline_first=str(BASE_FIRST), baseline_last=str(BASE_LAST),
                            frozen_at=datetime.now(KST).isoformat(), selection_reason=args.selection_reason,
                            thresholds=asdict(thresholds))
            with threshold_path.open("x", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
            print(threshold_path)
            return
        thresholds = load_thresholds(threshold_path)
        bars = load_bars(root / "raw" / f"{thresholds.symbol}.csv")
        trades_on = None if thresholds.symbol == thresholds.execution_symbol else load_bars(
            root / "raw" / f"{thresholds.execution_symbol}.csv")
        excluded = set(args.exclude_date)
        if not any(TEST_FIRST <= bar.start.date() <= TEST_LAST and bar.start.date() not in excluded for bar in bars):
            ap.error("No confirmed validation-period bars to replay")
        costs = Costs(thresholds.fee_per_side, thresholds.slippage_ticks, thresholds.tick_value)
        destination = root / "replay"
        portfolios, rows = replay(bars, thresholds, costs, trades_on, exclude_dates=excluded)
        export(destination, portfolios, rows, thresholds)
        export_sensitivity(destination, portfolios,
                           lambda ticks: replay(bars, thresholds, replace(costs, slippage_ticks=ticks), trades_on,
                                                exclude_dates=excluded)[0],
                           costs.slippage_ticks)
        from copper.compact import compact_results

        compact_results(destination)
        from copper.dashboard import render

        (destination / "dashboard.html").write_text(render(root, datetime.now(KST)), encoding="utf-8")
        (root / "dashboard.html").write_text(render(root, datetime.now(KST)), encoding="utf-8")
        print(destination)
        return

    from copper.market import api, connect, fetch_bars, quote_symbol

    try:
        session = connect(args.env_file)
    except ValueError as exc:
        ap.error(str(exc))
    try:
        if args.command == "collect":
            if args.last < args.first:
                ap.error("--last must be on or after --first")
            now = datetime.now(KST)
            quality = []
            for symbol in args.symbols:
                bars = fetch_bars(symbol, args.exchange, args.first, args.last, args.bar_stamp, now=now)
                write_csv(root / "raw" / f"{symbol}.csv", map(asdict, bars), RAW_FIELDS)
                quality.append(coverage(symbol, bars, args.first, args.last, now))
            write_csv(root / "raw" / "coverage.csv", quality,
                      ("symbol", "expected", "received", "volume", "zero_volume_ratio", "missing_ratio"))
            print(root / "raw")
        elif args.command == "inspect":
            from trader.market import DATE_FIELDS, TIME_FIELDS

            for symbol in args.symbols:
                day = args.date.strftime("%Y%m%d")
                report = {"symbol": symbol, "realtime_symbol": quote_symbol(symbol, args.exchange)}
                for minutes in ("1", "5"):
                    raw = api.chart_minute(symbol, exch_cd=args.exchange, inq_strt_dt=day, inq_ed_dt=day,
                                           dcnt=minutes, req_qty="15")
                    keys = (*DATE_FIELDS, *TIME_FIELDS, "occurs1_trd_qty", "occurs1_cum_trd_qty",
                            *(f"occurs1_{k}_pric" for k in ("open", "high", "low", "close")))
                    report[f"{minutes}m"] = [{k: row[k] for k in keys if k in row} for row in raw.get("Occurs1", [])]
                print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "quotes":
            destination = root / "liquidity" / datetime.now(KST).strftime("%Y%m%d_%H%M%S")
            asyncio.run(collect_quotes(destination, session, args.exchange, args.symbols, args.seconds))
            print(destination)
        else:
            from copper.live import run_live

            thresholds = load_thresholds(threshold_path)
            destination = root / args.command / datetime.now(KST).strftime("%Y%m%d_%H%M%S")
            asyncio.run(run_live(destination, session, thresholds, args.exchange, args.bar_stamp))
            print(destination)
    finally:
        session.close()


if __name__ == "__main__":
    main()
