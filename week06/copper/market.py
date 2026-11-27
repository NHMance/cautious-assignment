"""NH REST data collection using the clients shared with earlier weeks."""

import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .strategy import Bar, KST, STEP

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "week03"))
import trader  # Registers the Week 02 backend.
from backend.config import DEFAULT_BASE_URLS, Config, EnvironmentConfig, MarketConfig
from backend.nh_api import runtime
from backend.nh_api.errors import GatewayError
from backend.nh_api.session import DEFAULT_TIMEOUT, create_session
from backend.overseas_futureoption import overseas_futureoption_functions as api
from backend.overseas_futureoption.overseas_history import at_end
from bot.credentials import load_env_file
from bot.market import number
from trader.market import DATE_FIELDS, TIME_FIELDS, _first


def connect(env_file, *, timeout=DEFAULT_TIMEOUT):
    import os

    load_env_file(env_file)
    environment = "paper"
    prefix = "NH_PAPER_OVERSEAS_APP_"
    key, secret = (os.environ.get(prefix + name, "").strip() for name in ("KEY", "SECRET"))
    if not key or not secret:
        raise ValueError(f"Set {prefix}KEY and {prefix}SECRET in {env_file}")
    config = EnvironmentConfig(environment, DEFAULT_BASE_URLS[environment],
                               MarketConfig(environment, "domestic", "", ""),
                               MarketConfig(environment, "overseas", key, secret))
    session = create_session(config=Config(environment, {environment: config}), timeout=timeout)
    runtime.use(session)
    return session


def quote_symbol(symbol, exchange):
    rows = api.price([(exchange, symbol)]).get("Occurs1") or []
    row = rows[0] if rows else {}
    realtime_symbol = str(row.get("occurs1_real_sym") or "").strip()
    if not realtime_symbol:
        raise ValueError(f"NH REST did not return a realtime symbol for {symbol}")
    return realtime_symbol


def parse_bar(row, stamp):
    day = _first(row, DATE_FIELDS)
    clock = _first(row, TIME_FIELDS)
    if not day or not clock:
        raise ValueError("NH REST bar has no date or time")
    start = datetime.strptime(day[:8] + clock.replace(":", "").zfill(6), "%Y%m%d%H%M%S").replace(tzinfo=KST)
    if stamp == "end":
        start -= STEP
    values = [number(row.get(f"occurs1_{field}_pric")) for field in ("open", "high", "low", "close")]
    volume = number(row.get("occurs1_trd_qty"))
    if volume is None:
        volume = number(row.get("occurs1_cum_trd_qty"))
    if any(price is None for price in values) or volume is None or volume != int(volume):
        raise ValueError("NH REST bar has invalid OHLCV fields")
    return Bar(start, *values, int(volume))


def fetch_bars(symbol, exchange, first, last, stamp, *, now=None, on_retry=None):
    now = now or datetime.now(KST)
    result = {}
    # End-labelled midnight belongs to the previous day.
    last_request = min(now.date(), last + timedelta(days=stamp == "end"))
    if first > last_request:
        return []
    cursor = api.FIRST_PAGE
    seen = set()
    while cursor:
        if cursor in seen:
            raise ValueError(f"Repeated NH REST pagination cursor for {symbol}")
        seen.add(cursor)
        for delay in (1, 2, 4, None):
            try:
                raw = api.chart_minute(symbol, exch_cd=exchange, inq_strt_dt=first.strftime("%Y%m%d"),
                                       inq_ed_dt=last_request.strftime("%Y%m%d"), dcnt="5", next_key=cursor)
                break
            except GatewayError as exc:
                if exc.code != "EGW036" or delay is None:
                    raise
                if on_retry:
                    on_retry(exc, delay)
                time.sleep(delay)
        for row in raw.get("Occurs1") or []:
            bar = parse_bar(row, stamp)
            if first <= bar.start.date() <= last and bar.end <= now:
                result.setdefault(bar.start, bar)
        cursor = str(raw.get("nxt_key") or "").strip()
        if at_end(cursor):
            break
    return [result[key] for key in sorted(result)]


def load_bars(path):
    from .results import read_rows

    bars = [Bar(datetime.fromisoformat(r["start"]), *(float(r[k]) for k in ("open", "high", "low", "close")),
                int(r["volume"])) for r in read_rows(path)]
    if len({b.start for b in bars}) != len(bars):
        raise ValueError(f"Duplicate bar timestamps in {path}")
    now = datetime.now(KST)
    return sorted((b for b in bars if b.end <= now), key=lambda b: b.start)
