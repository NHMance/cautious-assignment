"""Independent portfolio accounting and OHLCV replay."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .strategy import (FORCE_CLOSE, TICK, TEST_FIRST, TEST_LAST,
                       IndicatorSeries, Signal, SignalEdges, entry_problem, strategy_exit)


@dataclass
class Position:
    signal: Signal
    at: datetime
    price: float
    reference: float | None

    @property
    def stop(self):
        return self.price - self.signal.side * self.signal.stop_ticks * TICK


class Portfolio:
    def __init__(self, variant, thresholds, costs):
        self.variant = variant
        self.thresholds = thresholds
        self.costs = costs
        self.edges = SignalEdges(variant)
        self.position = None
        self.realized = {}
        self.entries = {}
        self.trades = []
        self.holds = []

    def check(self, signal, now, price_available, spread=None):
        day = now.date()
        return entry_problem(self.variant, signal, now, self.realized.get(day, 0),
                             self.entries.get(day, 0), price_available, spread,
                             self.thresholds, self.costs)

    def hold(self, signal, reason):
        self.holds.append(dict(at=signal.at, strategy=self.variant, side=signal.side, reason=reason))

    def enter(self, signal, at, price, reference=None):
        self.position = Position(signal, at, price, reference)
        self.entries[at.date()] = self.entries.get(at.date(), 0) + 1

    def exit(self, at, price, reason, reference=None):
        pos = self.position
        gross = round((price - pos.price) / TICK * self.thresholds.tick_value * pos.signal.side, 8)
        fees = 2 * self.costs.fee_per_side
        net = gross - fees
        slippage = None
        if reference is not None and pos.reference is not None:
            slippage = round(((pos.price - pos.reference) + (reference - price)) / TICK
                             * self.thresholds.tick_value * pos.signal.side, 8)
        self.realized[at.date()] = self.realized.get(at.date(), 0) + net
        self.trades.append(dict(strategy=self.variant, regime=pos.signal.regime, rule=pos.signal.rule,
                                side=pos.signal.side, signal_at=pos.signal.at, entry_at=pos.at,
                                entry_price=pos.price, exit_at=at, exit_price=price, reason=reason,
                                gross=gross, fees=fees, slippage=slippage, net=net,
                                budget_exceeded=-net > self.thresholds.trade_loss_budget))
        self.position = None

    def timed_exit(self, at, *, force=FORCE_CLOSE, max_hold=timedelta(minutes=60)):
        pos = self.position
        if at.date() > pos.at.date() or at.time() >= force:
            return "EXIT_FORCE"
        if self.variant != "A-unlimited" and at - pos.at >= max_hold:
            return "EXIT_TIME"
        return None


def replay(bars, thresholds, costs, execution_bars=None, *, first=TEST_FIRST, last=TEST_LAST, exclude_dates=()):
    execution = {bar.start: bar for bar in (bars if execution_bars is None else execution_bars)}
    portfolios = [Portfolio(v, thresholds, costs) for v in ("A", "B", "C")]
    series = IndicatorSeries()
    slip = costs.slippage_ticks * TICK
    pending = {p.variant: (None, None) for p in portfolios}
    # Include execution-only bars so protective exits still run when a proxy bar is missing.
    signals = {bar.start: bar for bar in bars}
    rows = []
    for start in sorted(signals.keys() | execution.keys()):
        if start.date() > last:
            break
        bar = signals.get(start)
        trade_bar = execution.get(start)
        row = series.push(bar) if bar else None
        in_period = first <= start.date() <= last and start.date() not in exclude_dates
        if in_period and bar:
            rows.append((bar, row, thresholds.regime(bar, row)))
        for p in portfolios:
            edges = p.edges.push(bar, row, thresholds) if bar else []
            if not in_period and p.position is None:
                pending[p.variant] = (None, None)
                continue
            signal, exit_at = pending[p.variant]
            pending[p.variant] = (None, exit_at)
            if trade_bar and p.position:
                pos = p.position
                side = pos.signal.side
                if (trade_bar.open - pos.stop) * side <= 1e-10:
                    p.exit(start, trade_bar.open - side * slip, "EXIT_STOP", trade_bar.open)
                elif reason := p.timed_exit(start):
                    p.exit(start, trade_bar.open - side * slip, reason, trade_bar.open)
                elif exit_at is not None and start >= exit_at:
                    p.exit(start, trade_bar.open - side * slip, "EXIT_STRATEGY", trade_bar.open)
            if not p.position:
                pending[p.variant] = (None, None)
                if signal and in_period:
                    valid = trade_bar is not None and start == signal.at
                    if valid and execution_bars is not None:
                        valid = trade_bar.volume > 0
                    reason = p.check(signal, signal.at, valid)
                    if reason:
                        p.hold(signal, reason)
                    else:
                        p.enter(signal, start, trade_bar.open + signal.side * slip, trade_bar.open)
            if trade_bar and p.position:
                pos = p.position
                side = pos.signal.side
                worst = trade_bar.low if side == 1 else trade_bar.high
                if (worst - pos.stop) * side <= 1e-10:
                    p.exit(trade_bar.end, pos.stop - side * slip, "EXIT_STOP", pos.stop)
                    pending[p.variant] = (None, None)
            exit_at = pending[p.variant][1]
            if bar and p.position and strategy_exit(p.position, row):
                exit_at = exit_at or bar.end
            if in_period and edges and (p.position is None or exit_at is not None
                                        or p.timed_exit(bar.end) is not None):
                pending[p.variant] = (edges[0], exit_at)
            else:
                pending[p.variant] = (None, exit_at)
    for p in portfolios:
        signal, _ = pending[p.variant]
        if signal and not p.position:
            p.hold(signal, p.check(signal, signal.at, False) or "HOLD_STALE")
    return portfolios, rows
