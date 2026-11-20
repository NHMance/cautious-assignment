"""Reconcile missing HG five-minute intervals against NH one-minute bars."""

import csv
import json
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .market import (api, at_end, connect, GatewayError, _first, DATE_FIELDS,
                     TIME_FIELDS, number, fetch_bars)
from .results import write_csv
from .strategy import BASE_FIRST, BASE_LAST, ENTRY_OPEN, FORCE_CLOSE, KST, STEP

ROOT = Path(__file__).resolve().parents[1] / "results"


def collect_minutes():
    cursor = api.FIRST_PAGE
    seen = set()
    result = {}
    while cursor:
        if cursor in seen:
            raise ValueError("Repeated minute pagination cursor")
        seen.add(cursor)
        for delay in (1, 2, 4, None):
            try:
                raw = api.chart_minute("HGH27", exch_cd="FCME", inq_strt_dt="20261102",
                                       inq_ed_dt="20261113", dcnt="1", next_key=cursor)
                break
            except GatewayError as exc:
                if exc.code != "EGW036" or delay is None:
                    raise
                time.sleep(delay)
        for row in raw.get("Occurs1") or []:
            stamp = datetime.strptime(_first(row, DATE_FIELDS) + _first(row, TIME_FIELDS),
                                      "%Y%m%d%H%M%S").replace(tzinfo=KST)
            volume = number(row.get("occurs1_trd_qty"))
            if volume is None:
                volume = number(row.get("occurs1_cum_trd_qty"))
            if volume is None:
                raise ValueError("Missing minute volume")
            if BASE_FIRST <= stamp.date() <= BASE_LAST:
                result.setdefault(stamp, dict(start=stamp, volume=volume))
        cursor = str(raw.get("nxt_key") or "").strip()
        if at_end(cursor):
            break
        print(f"Minute page {len(seen)}, baseline minutes {len(result)}", flush=True)
    return result, len(seen)


def main():
    session = connect(ROOT.parent / ".env")
    try:
        minutes, pages = collect_minutes()
        five = {bar.start: bar for bar in fetch_bars("HGH27", "FCME", BASE_FIRST, BASE_LAST, "start")}
    finally:
        session.close()
    if not minutes or min(minutes).date() > BASE_FIRST or max(minutes).date() < BASE_LAST:
        raise ValueError("Incomplete baseline minute history")
    folder = ROOT / "data-verification"
    write_csv(folder / "HGH27_1m_baseline.csv", (minutes[t] for t in sorted(minutes)), ("start", "volume"))
    windows = []
    day = BASE_FIRST
    while day <= BASE_LAST:
        if day.weekday() < 5:
            start = datetime.combine(day, ENTRY_OPEN, KST)
            while start < datetime.combine(day, FORCE_CLOSE, KST):
                rows = [minutes[t] for i in range(5) if (t := start + timedelta(minutes=i)) in minutes]
                old = five.get(start)
                windows.append(dict(start=start, five_present=old is not None, minutes_present=len(rows),
                                    minute_volume=sum(r["volume"] for r in rows),
                                    five_volume=old.volume if old else None))
                start += STEP
        day += timedelta(days=1)
    write_csv(folder / "HGH27_gap_reconciliation.csv", windows, tuple(windows[0]))
    missing = [r for r in windows if not r["five_present"]]
    summary = dict(checked_at=datetime.now(KST).isoformat(), expected=len(windows),
                   five_present=len(windows) - len(missing), five_missing=len(missing),
                   missing_with_minute_trades=sum(r["minute_volume"] > 0 for r in missing),
                   missing_without_minutes=sum(r["minutes_present"] == 0 for r in missing),
                   present_without_minutes=sum(r["five_present"] and r["minutes_present"] == 0 for r in windows),
                   volume_mismatches=sum(r["five_present"] and r["five_volume"] != r["minute_volume"] for r in windows),
                   one_minute_records=len(minutes), minute_pages=pages,
                   by_day={str(day): dict(expected=len(rows), missing=sum(not r["five_present"] for r in rows))
                           for day in sorted({r["start"].date() for r in windows})
                           if (rows := [r for r in windows if r["start"].date() == day])})
    (folder / "HGH27_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
