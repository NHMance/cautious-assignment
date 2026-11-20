"""WebSocket execution prices with REST bar and paper-account reconciliation."""

import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

from .market import fetch_bars, quote_symbol
from .replay import Portfolio
from .results import export, export_spreads
from .strategy import BASE_FIRST, FORCE_CLOSE, KST, STEP, TICK, IndicatorSeries, SignalEdges
from .strategy import Costs, strategy_exit

from bot.clock import RealClock
from bot.journal import Journal
from bot.market import number
from trader.broker import OverseasBroker
from trader.credentials import DEFAULT_WS_URLS
from trader.realtime import FA, FB, NHStream, Subscription, action_code_of, field, parse_tick, session_credentials
from trader.strategy import LONG, SHORT

QUOTE_AGE = timedelta(seconds=10)


@dataclass(frozen=True)
class Quote:
    at: datetime
    bid: float
    ask: float

    @property
    def spread(self):
        return (self.ask - self.bid) / TICK


def ws_symbol(msg):
    header = msg.get("header") or {}
    return str(field(msg, "sym") or field(msg, "action_sym") or header.get("action_sym") or "").strip()


def parse_quote(msg, now):
    if action_code_of(msg) == FB:
        bid, ask = (number(field(msg, key)) for key in ("bid_pric_1", "ask_pric_1"))
    else:
        bid, ask = (number(field(msg, key)) for key in ("bid_best_pric", "ask_best_pric"))
    if bid is not None and ask is not None and isfinite(bid) and isfinite(ask) and 0 < bid <= ask:
        return Quote(now, bid, ask)
    return None


class Feed:
    def __init__(self, symbols, session, quote_writer, journal):
        self.symbols = symbols
        self.quotes = {}
        self.ticks = {}
        self.stop = None
        self.quote_writer = quote_writer
        self.journal = journal
        self.stream = NHStream(DEFAULT_WS_URLS["paper"], session_credentials(session, environment="paper"),
                               [Subscription(kind, code) for code in symbols for kind in (FA, FB)],
                               self.data, self.status)

    def status(self, event, detail):
        if event in ("connected", "disconnected", "fatal", "error"):
            self.quotes.clear()
            self.ticks.clear()
        self.journal.write("WS_STATUS", event=event)

    def data(self, msg):
        now = datetime.now(KST)
        code = ws_symbol(msg)
        if not code and len(self.symbols) == 1:
            code = next(iter(self.symbols))
        if code not in self.symbols:
            return
        symbol = self.symbols[code]
        kind = action_code_of(msg)
        if kind not in (FA, FB):
            return
        quote = parse_quote(msg, now)
        if quote:
            self.quotes[symbol] = quote
            self.quote_writer.writerow((now.isoformat(), symbol, quote.bid, quote.ask, quote.spread, kind))
        elif kind == FB:
            self.quotes.pop(symbol, None)
        if kind == FA and (tick := parse_tick(msg, received=now)) and isfinite(tick.price):
            self.ticks[symbol] = tick
            if self.stop:
                stop, side, _ = self.stop
                if (tick.price - stop) * side <= 1e-10:
                    self.stop = (stop, side, True)

    def quote(self, symbol, now):
        quote = self.quotes.get(symbol)
        return quote if quote and now - quote.at <= QUOTE_AGE else None

    def tradable(self, symbol, now):
        tick = self.ticks.get(symbol)
        return self.quote(symbol, now) if tick and now - tick.received <= QUOTE_AGE else None


def fill_timestamp(row):
    stamp = str(row.get("occurs1_exec_tm") or "").strip()
    day = str(row.get("occurs1_exec_dt") or "").strip()
    if len(stamp) in (6, 9) and len(day) == 8:
        stamp = day + stamp
    for pattern in ("%Y%m%d%H%M%S", "%Y%m%d%H%M%S%f"):
        if (pattern.endswith("%f") and 14 < len(stamp) <= 20) or len(stamp) == 14:
            try:
                return datetime.strptime(stamp, pattern).replace(tzinfo=KST)
            except ValueError:
                continue
    return None


class PaperExecution:
    def __init__(self, symbol, journal):
        self.journal = journal
        self.broker = OverseasBroker(symbol, journal, RealClock())
        self.uncertain = False

    def assert_position(self, side):
        if self.broker.pending_count():
            raise RuntimeError("NH REST reports pending orders on the execution symbol")
        actual = self.broker.position()
        expected = None if side == 0 else (LONG if side == 1 else SHORT, 1)
        if (None if actual is None else (actual.side, actual.qty)) != expected:
            raise RuntimeError("NH REST position differs from the strategy position")

    def execute(self, side, price, *, entry):
        self.assert_position(0 if entry else -side)
        side_name = LONG if side == 1 else SHORT
        role = "ENTRY" if entry else "EXIT"
        if entry and (self.broker.orderable_qty(side_name, price) or 0) < 1:
            return None
        self.uncertain = True
        requested = datetime.now(KST)
        self.journal.write("ORDER_REQUEST", role=role, at=requested.isoformat(), side=side)
        order = self.broker.submit(side_name, 1, role=role)
        fill = self.broker.wait_fill(order, side=side_name, qty=1, ref_price=price, role=role) if order else None
        # An unknown submission stays locked even if REST currently reports flat.
        if fill is None or fill.qty != 1:
            raise RuntimeError("Paper order outcome requires REST reconciliation before another order")
        confirmed = datetime.now(KST)
        self.assert_position(side if entry else 0)
        matching = [r for r in self.broker.history()
                    if str(r.get("occurs1_ord_no") or "").strip().lstrip("0") == order]
        times = [at for row in matching if (at := fill_timestamp(row)) is not None]
        at = max(times) if times else confirmed
        self.journal.write("FILL_TIMING", order_no=order, at=at.isoformat(),
                           source="NH_REST" if times else "REST_CONFIRMATION")
        self.uncertain = False
        return at, fill.price


def execution_reference(quote, side):
    return quote.ask if side == 1 else quote.bid


async def run_live(root, session, thresholds, exchange, stamp):
    symbol = thresholds.execution_symbol
    journal = Journal(symbol, root=root)
    root.mkdir(parents=True, exist_ok=True)
    execution = PaperExecution(symbol, journal)
    await asyncio.to_thread(execution.assert_position, 0)
    code = await asyncio.to_thread(quote_symbol, symbol, exchange)
    now = datetime.now(KST)
    day = now.date()
    cutoff = datetime.combine(day, FORCE_CLOSE, KST)
    if now >= cutoff:
        raise ValueError("The KST trading window has already closed")
    costs = Costs(thresholds.fee_per_side, thresholds.slippage_ticks, thresholds.tick_value)
    p = Portfolio("C", thresholds, costs)
    series = IndicatorSeries()
    history = await asyncio.to_thread(fetch_bars, thresholds.symbol, exchange, BASE_FIRST, day, stamp, now=now)
    rows = []
    for bar in history:
        row = series.push(bar)
        p.edges.push(bar, row, thresholds)
        if bar.start.date() == day:
            rows.append((bar, row, thresholds.regime(bar, row)))
    last = history[-1].end if history else None
    known = {bar.start: bar for bar in history}
    task = None
    shutting_down = False
    next_rest = datetime.now(KST)
    with (root / "quotes.csv").open("w", encoding="utf-8", newline="", buffering=1) as handle:
        writer = csv.writer(handle)
        writer.writerow(("at", "symbol", "bid", "ask", "spread_ticks", "channel"))
        stop = asyncio.Event()
        feed = Feed({code: symbol}, session, writer, journal)
        stream_task = asyncio.create_task(feed.stream.run(stop))

        async def paper_fill(side, ref, *, entry):
            nonlocal shutting_down
            order = asyncio.create_task(asyncio.to_thread(execution.execute, side, ref, entry=entry))
            try:
                return await asyncio.shield(order)
            except asyncio.CancelledError:
                shutting_down = True
                return await order

        async def close(reason, now):
            if execution.uncertain:
                return False
            side = -p.position.signal.side
            quote = feed.quote(symbol, now)
            ref = execution_reference(quote, side) if quote else None
            fill = await paper_fill(side, ref, entry=False)
            p.exit(*fill, reason, ref)
            journal.write("TRADE", **p.trades[-1])
            feed.stop = None
            return True

        try:
            while not shutting_down:
                now = datetime.now(KST)
                if stream_task.done():
                    raise RuntimeError("NH WebSocket stopped")
                if now >= cutoff and p.position is None:
                    break
                if p.position:
                    stopped = feed.stop and feed.stop[2]
                    if reason := ("STOP" if stopped else p.timed_exit(now)):
                        await close(reason, now)
                if task is None and now >= next_rest and now < cutoff:
                    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
                    task = asyncio.create_task(asyncio.to_thread(fetch_bars, thresholds.symbol, exchange,
                                                                   day, day, stamp, now=now))
                    next_rest = boundary + STEP
                if task and task.done():
                    try:
                        bars = task.result()
                    except Exception as exc:
                        journal.error("overseas_chart-minute", exc)
                        bars = []
                    task = None
                    now = datetime.now(KST)
                    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
                    if not bars or bars[-1].end < boundary:
                        next_rest = min(next_rest, now + timedelta(seconds=5))
                    corrections = [bar for bar in bars if last and bar.end <= last and known.get(bar.start) != bar]
                    if corrections:
                        journal.write("BAR_RECONCILE", timestamps=[bar.start.isoformat() for bar in corrections])
                        known.update((bar.start, bar) for bar in corrections)
                        series = IndicatorSeries()
                        rows.clear()
                        p.edges = SignalEdges(p.variant)
                        for start in sorted(known):
                            old = known[start]
                            row = series.push(old)
                            p.edges.push(old, row, thresholds)
                            if start.date() == day:
                                rows.append((old, row, thresholds.regime(old, row)))
                    for bar in bars:
                        if last is not None and bar.end <= last:
                            continue
                        if last and bar.start > last:
                            journal.write("BAR_GAP", previous_end=last, next_start=bar.start)
                        last = bar.end
                        known[bar.start] = bar
                        row = series.push(bar)
                        regime = thresholds.regime(bar, row)
                        rows.append((bar, row, regime))
                        journal.write("BAR", start=bar.start, end=bar.end, regime=regime)
                        edges = p.edges.push(bar, row, thresholds)
                        if bar.end != boundary:
                            continue
                        if p.position and strategy_exit(p.position, row):
                            await close("STRATEGY", datetime.now(KST))
                        if shutting_down or p.position or not edges:
                            continue
                        signal = edges[0]
                        at = datetime.now(KST)
                        quote = feed.tradable(symbol, at)
                        reason = p.check(signal, at, quote is not None, quote.spread if quote else None)
                        if reason:
                            p.hold(signal, reason)
                            journal.write("HOLD", **p.holds[-1])
                            continue
                        price = execution_reference(quote, signal.side)
                        fill = await paper_fill(signal.side, price, entry=True)
                        if fill is None:
                            p.hold(signal, "NOT_ORDERABLE")
                            journal.write("HOLD", **p.holds[-1])
                            continue
                        p.enter(signal, *fill, price)
                        journal.write("ENTRY", strategy=p.variant, signal_at=signal.at, at=fill[0],
                                      side=signal.side, price=fill[1], rule=signal.rule, regime=signal.regime,
                                      stop=p.position.stop)
                        latest = feed.ticks.get(symbol)
                        stop_price = p.position.stop
                        breached = bool(latest and latest.received >= fill[0]
                                        and (latest.price - stop_price) * signal.side <= 1e-10)
                        feed.stop = (stop_price, signal.side, breached)
                await asyncio.sleep(0.2)
        finally:
            try:
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                if p.position and not await close("SHUTDOWN", datetime.now(KST)):
                    journal.write("POSITION_OPEN", strategy=p.variant)
            finally:
                stop.set()
                await asyncio.gather(stream_task, return_exceptions=True)
                handle.flush()
                export(root, [p], rows, thresholds)
                export_spreads(root, root / "quotes.csv")
                from .compact import compact_results

                compact_results(root)
