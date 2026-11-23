"""Confirmed-bar indicators, calibration and entry/exit rules."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import ceil, isfinite
from statistics import mean, median, quantiles, stdev

KST = timezone(timedelta(hours=9))
STEP = timedelta(minutes=5)
TICK = 0.0005
TICK_VALUE = 1.25
BASE_FIRST, BASE_LAST = date(2026, 11, 9), date(2026, 11, 20)
TEST_FIRST, TEST_LAST = date(2026, 11, 23), date(2026, 11, 27)
ENTRY_OPEN, ENTRY_CLOSE = time(17), time(22, 20)
FORCE_CLOSE, RUN_END = time(22, 45), time(23, 30)


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self):
        if self.start.utcoffset() is None:
            raise ValueError("Bar timestamps must include a timezone")
        object.__setattr__(self, "start", self.start.astimezone(KST))
        if self.start.minute % 5 or self.start.second or self.start.microsecond:
            raise ValueError("Bar start must be on the five-minute grid")
        if (not all(isfinite(p) and p > 0 for p in (self.open, self.high, self.low, self.close))
                or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high
                or self.volume < 0):
            raise ValueError("Invalid OHLCV")

    @property
    def end(self):
        return self.start + STEP


@dataclass(frozen=True)
class Costs:
    fee_per_side: float
    slippage_ticks: int
    tick_value: float = TICK_VALUE

    def __post_init__(self):
        if not isfinite(self.fee_per_side) or self.fee_per_side < 0 or self.slippage_ticks < 0:
            raise ValueError("Costs must be finite and nonnegative")

    @property
    def round_trip(self):
        return 2 * (self.fee_per_side + self.slippage_ticks * self.tick_value)


@dataclass(frozen=True)
class Indicators:
    ema9: float | None
    ema21: float | None
    atr: float | None
    er: float | None
    z: float | None


class IndicatorSeries:
    def __init__(self):
        self.closes = deque(maxlen=24)
        self.initial_tr = []
        self.atr = self.ema9 = self.ema21 = None

    def push(self, bar):
        close = bar.close
        previous = list(self.closes)
        tr = bar.high - bar.low
        if previous:
            tr = max(tr, abs(bar.high - previous[-1]), abs(bar.low - previous[-1]))
        if self.atr is None:
            self.initial_tr.append(tr)
            if len(self.initial_tr) == 14:
                self.atr = mean(self.initial_tr)
        else:
            self.atr = (13 * self.atr + tr) / 14
        if not previous:
            self.ema9 = self.ema21 = close
        else:
            self.ema9 += (close - self.ema9) * (2 / 10)
            self.ema21 += (close - self.ema21) * (2 / 22)
        er = z = None
        if len(previous) == 24:
            closes = previous + [close]
            travel = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
            er = abs(close - previous[0]) / travel if travel else 0.0
            deviation = stdev(previous)
            z = (close - mean(previous)) / deviation if deviation else 0.0
        self.closes.append(close)
        return Indicators(self.ema9 if len(self.closes) >= 9 else None,
                          self.ema21 if len(self.closes) >= 21 else None, self.atr, er, z)


def time_bucket(bar):
    return bar.start.strftime("%H:") + ("00" if bar.start.minute < 30 else "30")


def stop_ticks(atr, minimum=8, atr_multiple=1.5):
    return max(minimum, ceil(round(atr_multiple * atr / TICK, 10)))


@dataclass(frozen=True)
class Thresholds:
    symbol: str
    execution_symbol: str
    er_hi: float
    er_lo: float
    rv_hi: float
    atr_medians: dict[str, float]
    trade_loss_budget: float
    daily_loss_limit: float
    fee_per_side: float
    slippage_ticks: int

    @property
    def tick_value(self):
        return 12.5 if self.execution_symbol.startswith('HG') else TICK_VALUE

    def relative_atr(self, bar, indicators):
        baseline = self.atr_medians.get(time_bucket(bar), 0)
        if baseline > 0 and indicators.atr is not None:
            return indicators.atr / baseline
        return None

    def regime(self, bar, indicators):
        relative = self.relative_atr(bar, indicators)
        if relative is None or indicators.er is None:
            return None
        if relative >= self.rv_hi:
            return "HIGH_VOL"
        if indicators.er >= self.er_hi:
            return "TREND"
        if indicators.er <= self.er_lo:
            return "RANGE"
        return "NEUTRAL"


def calibrate(bars, costs, *, symbol, execution_symbol, first=BASE_FIRST, last=BASE_LAST):
    tick_value = 12.5 if execution_symbol.startswith('HG') else TICK_VALUE
    costs = Costs(costs.fee_per_side, costs.slippage_ticks, tick_value)
    series = IndicatorSeries()
    samples = [(bar, series.push(bar)) for bar in bars if bar.start.date() <= last]
    base = [(bar, row) for bar, row in samples if bar.start.date() >= first]
    buckets = defaultdict(list)
    for bar, row in base:
        if row.atr is not None:
            buckets[time_bucket(bar)].append(row.atr)
    baselines = {key: median(values) for key, values in buckets.items()}
    ers = [row.er for _, row in base if row.er is not None]
    relative = [row.atr / baselines[time_bucket(bar)] for bar, row in base
                if row.atr is not None and baselines[time_bucket(bar)] > 0]
    stops = [stop_ticks(row.atr) * tick_value for _, row in base if row.atr is not None]
    if min(len(ers), len(relative), len(stops)) < 2:
        raise ValueError("Insufficient calibration data")
    er_quantiles = quantiles(ers, n=10, method="inclusive")
    budget = 5 * ceil((quantiles(stops, n=10, method="inclusive")[7] + costs.round_trip) / 5)
    return Thresholds(symbol, execution_symbol, er_quantiles[6], er_quantiles[2],
                      quantiles(relative, n=10, method="inclusive")[8], baselines,
                      budget, 3 * budget, costs.fee_per_side, costs.slippage_ticks)


@dataclass(frozen=True)
class Signal:
    at: datetime
    side: int
    rule: str
    regime: str | None
    stop_ticks: int | None


class SignalEdges:
    def __init__(self, variant):
        self.variant = variant
        self.previous = set()

    def push(self, bar, row, thresholds):
        trend = row.ema21 is not None and row.ema9 != row.ema21
        reversion = row.z is not None and abs(row.z) >= 1.5
        adaptive = self.variant in ("C", "C-1")
        trend_regime = adaptive and row.er is not None and row.er >= thresholds.er_hi
        range_regime = adaptive and row.er is not None and not trend_regime and row.er <= thresholds.er_lo
        active = []
        if trend and (self.variant in ("A", "A-unlimited") or trend_regime):
            active.append(("TREND", 1 if row.ema9 > row.ema21 else -1))
        elif reversion and (self.variant == "B" or range_regime):
            active.append(("RANGE", 1 if row.z < 0 else -1))
        edges = [item for item in active if item not in self.previous]
        self.previous = set(active)
        return [Signal(bar.end, side, rule, thresholds.regime(bar, row),
                       stop_ticks(row.atr) if row.atr is not None else None) for rule, side in edges]


def strategy_exit(position, row):
    if position.signal.rule == "TREND":
        return row.ema21 is not None and (row.ema9 - row.ema21) * position.signal.side < 0
    return row.z is not None and row.z * position.signal.side >= -0.25


def entry_problem(variant, signal, now, daily_pnl, entries, price_available, spread_ticks, thresholds, costs):
    clock = now.astimezone(KST).time()
    if not ENTRY_OPEN <= clock < ENTRY_CLOSE:
        return "HOLD_ENTRY_TIME"
    if daily_pnl <= -thresholds.daily_loss_limit:
        return "HOLD_DAILY_LOSS"
    if entries >= 5:
        return "HOLD_MAX_TRADES"
    if not price_available:
        return "HOLD_STALE"
    if variant == "C" and signal.regime == "HIGH_VOL":
        return "HOLD_HIGH_VOL"
    if spread_ticks is not None and spread_ticks > 3 + 1e-9:
        return "HOLD_SPREAD"
    if signal.stop_ticks is None or signal.regime is None:
        return "HOLD_INDICATOR_UNAVAILABLE"
    if signal.stop_ticks * thresholds.tick_value + costs.round_trip > thresholds.trade_loss_budget:
        return "HOLD_BUDGET"
    return None
