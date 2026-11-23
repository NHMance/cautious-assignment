"""CSV exports and validation statistics."""

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, time
from statistics import mean, median

from .strategy import STEP, TICK_VALUE

TRADE_FIELDS = ("strategy", "regime", "rule", "side", "signal_at", "entry_at", "entry_price", "exit_at",
                "exit_price", "reason", "gross", "fees", "slippage", "net", "budget_exceeded")
HOLD_FIELDS = ("at", "strategy", "side", "reason")
BAR_FIELDS = ("start", "open", "high", "low", "close", "volume", "ema9", "ema21", "atr", "er", "z",
              "relative_atr", "regime")


def write_csv(path, rows, fields):
    """Store a named table in the directory's consolidated JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.parent / "results.json"
    tables = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    tables[path.stem] = [{key: "" if row.get(key) is None else str(row[key]) for key in fields} for row in rows]
    working = target.with_suffix(".tmp")
    working.write_text(json.dumps(tables, ensure_ascii=False, default=str), encoding="utf-8")
    working.replace(target)


def read_rows(path):
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    target = path.parent / "results.json"
    return json.loads(target.read_text(encoding="utf-8")).get(path.stem, []) if target.is_file() else []


def metrics(pnls):
    equity = peak = drawdown = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(net=equity, trades=len(pnls), win_rate=sum(v > 0 for v in pnls) / len(pnls) if pnls else 0,
                mdd=drawdown, avg_trade=mean(pnls) if pnls else 0)


def signal_analysis(rows):
    groups = defaultdict(list)
    next_sample = None
    for i, (bar, row, regime) in enumerate(rows[:-12]):
        future = rows[i + 12][0]
        if future.start - bar.start != 12 * STEP or not regime or not row.atr:
            continue
        change = (future.close - bar.close) / row.atr
        trend = row.ema9 - row.ema21 if row.ema21 is not None else 0
        trend_value = change * (1 if trend > 0 else -1) if trend else None
        range_value = change * (-1 if row.z > 0 else 1) if row.z is not None and abs(row.z) >= 1.5 else None
        nonoverlap = next_sample is None or bar.start >= next_sample
        if nonoverlap:
            next_sample = future.start
        for rule, value in (("TREND", trend_value), ("RANGE", range_value)):
            if value is None:
                continue
            cohorts = ["eligible"]
            if trend_value is not None and range_value is not None:
                cohorts.append("matched")
            for cohort in cohorts:
                groups[regime, rule, cohort, "all"].append(value)
                if nonoverlap:
                    groups[regime, rule, cohort, "nonoverlap"].append(value)
    for (regime, rule, cohort, sample), values in sorted(groups.items()):
        yield dict(regime=regime, rule=rule, cohort=cohort, sample=sample, mean=mean(values),
                   median=median(values), positive=sum(v > 0 for v in values) / len(values), count=len(values))


def export(root, portfolios, rows, thresholds):
    write_csv(root / "bars.csv", (dict(**asdict(bar), **asdict(row), regime=regime,
                                      relative_atr=thresholds.relative_atr(bar, row))
                                  for bar, row, regime in rows), BAR_FIELDS)
    write_csv(root / "trades.csv", (row for p in portfolios for row in p.trades), TRADE_FIELDS)
    write_csv(root / "holds.csv", (row for p in portfolios for row in p.holds), HOLD_FIELDS)
    open_positions = [dict(strategy=p.variant, entry_at=p.position.at, entry_price=p.position.price,
                           side=p.position.signal.side, stop=p.position.stop)
                      for p in portfolios if p.position]
    write_csv(root / "open_positions.csv", open_positions, ("strategy", "entry_at", "entry_price", "side", "stop"))
    summary = []
    for p in portfolios:
        regimes = {t["regime"] for t in p.trades}
        summary.append(dict(strategy=p.variant, regime="ALL", holds=len(p.holds),
                            **metrics([t["net"] for t in p.trades])))
        for regime in sorted(regimes):
            summary.append(dict(strategy=p.variant, regime=regime, holds=None,
                                **metrics([t["net"] for t in p.trades if t["regime"] == regime])))
    write_csv(root / "summary.csv", summary,
              ("strategy", "regime", "net", "trades", "win_rate", "mdd", "avg_trade", "holds"))
    regimes = Counter(regime or "UNAVAILABLE" for _, _, regime in rows)
    write_csv(root / "regimes.csv", (dict(regime=key, bars=count, minutes=count * 5)
                                     for key, count in sorted(regimes.items())), ("regime", "bars", "minutes"))
    write_csv(root / "signal_analysis.csv", signal_analysis(rows),
              ("regime", "rule", "cohort", "sample", "mean", "median", "positive", "count"))


def export_sensitivity(root, portfolios, rerun, default_ticks):
    rows = []
    for ticks in range(4):
        for p in portfolios:
            change = 2 * (ticks - default_ticks) * p.thresholds.tick_value
            rows.append(dict(strategy=p.variant, ticks=ticks, mode="reprice",
                             **metrics([t["net"] - change for t in p.trades])))
        for p in rerun(ticks):
            rows.append(dict(strategy=p.variant, ticks=ticks, mode="rerun",
                             **metrics([t["net"] for t in p.trades])))
    write_csv(root / "sensitivity.csv", rows,
              ("strategy", "ticks", "mode", "net", "trades", "win_rate", "mdd", "avg_trade"))


def export_spreads(root, path):
    groups = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            at = datetime.fromisoformat(row["at"]).time()
            session = "Asia" if time(9) <= at < time(17) else "London" if time(17) <= at < time(23, 30) else "US"
            ticks = round(float(row["spread_ticks"]), 8)
            for label in (session, "ALL"):
                groups[row["symbol"], label].append(ticks)
    summary = []
    distribution = []
    for (symbol, session), ticks in sorted(groups.items()):
        summary.append(dict(symbol=symbol, session=session, count=len(ticks), mean=mean(ticks), median=median(ticks)))
        for value, count in sorted(Counter(ticks).items()):
            distribution.append(dict(symbol=symbol, session=session, spread_ticks=value, count=count))
    write_csv(root / "spread_summary.csv", summary, ("symbol", "session", "count", "mean", "median"))
    write_csv(root / "spread_distribution.csv", distribution, ("symbol", "session", "spread_ticks", "count"))
