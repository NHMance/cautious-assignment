"""WebSocket execution prices with REST bar and paper-account reconciliation."""

import asyncio
import csv
import json
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from math import isfinite

import httpx

from .market import api, fetch_bars, quote_symbol
from backend.auth.auth_functions import AuthError
from backend.nh_api.errors import BusinessError, TransportError
from .replay import Portfolio
from .results import export, export_spreads
from .strategy import BASE_FIRST, ENTRY_OPEN, ENTRY_CLOSE, FORCE_CLOSE, RUN_END, KST, STEP, TICK
from .strategy import IndicatorSeries, SignalEdges, stop_ticks
from .strategy import Costs, strategy_exit

from bot.clock import RealClock
from bot.journal import Journal
from bot.market import number
from trader.broker import OverseasBroker, trd_code
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
                               self.data, self.status, idle_timeout=None, ping_interval=20)

    def status(self, event, detail):
        if event in ("connected", "disconnected", "fatal", "error"):
            self.quotes.clear()
            self.ticks.clear()
        fields = {}
        if event in ("disconnected", "fatal", "error", "reconnect_wait"):
            fields = {key: detail[key] for key in ("error", "attempt", "seconds") if key in detail}
        self.journal.write("WS_STATUS", event=event, **fields)

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
        quote = parse_quote(msg, now) if kind == FB else None
        if quote:
            self.quotes[symbol] = quote
            self.quote_writer.writerow((now.isoformat(), symbol, quote.bid, quote.ask, quote.spread, kind))
        elif kind == FB:
            self.quotes.pop(symbol, None)
        if kind == FA and (tick := parse_tick(msg, received=now)) and isfinite(tick.price):
            self.ticks[symbol] = tick
            if self.stop:
                stop, side, _ = self.stop
                if stop_breached(tick.price, stop, side):
                    self.stop = (stop, side, True)

    def quote(self, symbol, now):
        quote = self.quotes.get(symbol)
        return quote if quote and now - quote.at < QUOTE_AGE else None

    def tradable(self, symbol, now):
        tick = self.ticks.get(symbol)
        return self.quote(symbol, now) if tick and now - tick.received < QUOTE_AGE else None


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


@dataclass(frozen=True)
class RiskSettings:
    stop_minimum: int = 8
    stop_multiple: float = 1.5
    max_hold: timedelta = timedelta(minutes=60)
    force: time = FORCE_CLOSE

    def signal(self, signal, atr):
        return replace(signal, stop_ticks=stop_ticks(atr, self.stop_minimum, self.stop_multiple)) if atr is not None else signal

    def exit_reason(self, portfolio, now, stopped):
        timed = portfolio.timed_exit(now, force=self.force, max_hold=self.max_hold)
        if timed == "EXIT_FORCE":
            return timed
        return "EXIT_STOP" if stopped else timed

    def entry_reason(self, portfolio, signal, now, quote):
        if now.astimezone(KST).time() >= self.force:
            return "HOLD_ENTRY_TIME"
        return portfolio.check(signal, now, quote is not None, quote.spread if quote else None)


class TradingLocked(RuntimeError):
    pass


class OrderRejected(RuntimeError):
    pass


REST_CONNECTION_ERRORS = (TransportError, httpx.TransportError, OSError)


def connection_error_type(exc):
    while exc.__cause__ and isinstance(exc.__cause__, (*REST_CONNECTION_ERRORS, AuthError)):
        exc = exc.__cause__
    return type(exc).__name__


def lock_message(exc):
    return connection_error_type(exc) if isinstance(exc, REST_CONNECTION_ERRORS) else str(exc)


class RestJournal(Journal):
    def write(self, type_, **fields):
        record = super().write(type_, **fields)
        if type_ in {"ERROR", "ORDER_REJECT", "LOCK_UNKNOWN"}:
            with (self.root / "api-errors.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def error(self, api_name, exc, **fields):
        fields.setdefault("action", "조회 실패 기록. 주문 결과 확인이 불가능하면 주문 잠금")
        if isinstance(exc, REST_CONNECTION_ERRORS):
            return self.write("ERROR", api=api_name, kind=connection_error_type(exc), attempt=1, **fields)
        fields.setdefault("response", getattr(exc, "body", None))
        return super().error(api_name, exc, **fields)


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
        self.journal.write("ACCOUNT_CHECK", position_qty=abs(side), pending=0)

    def execute(self, side, price, *, entry, reason=None, clock=None, entry_until=ENTRY_CLOSE):
        if self.uncertain:
            raise TradingLocked("Manual reconciliation required")
        now = clock or (lambda: datetime.now(KST))
        side_name = LONG if side == 1 else SHORT
        role = "ENTRY" if entry else "EXIT"
        stage = "overseas_open-interest / overseas_order-history"
        try:
            self.assert_position(0 if entry else -side)
            if entry:
                stage = "overseas_orderable-quantity"
                if (self.broker.orderable_qty(side_name, price) or 0) < 1:
                    return None
                if not ENTRY_OPEN <= now().time() < entry_until:
                    return None
            stage = "overseas_order"
            self.uncertain = True
            self.journal.write("ORDER_REQUEST", role=role, side=side, reason=reason, qty=1)
            try:
                response = api.order(self.broker.symbol, qt_div_cd=api.NEW, trd_div_cd=trd_code(side_name),
                                     glb_ord_tp_cd=api.MARKET_PRICE, ord_qty="1", ord_pric="0", stop_pric="")
            except BusinessError as exc:
                self.reject(role, exc.code, exc.detail, exc.body)
            code = str(response.get("err_cd") or "").strip()
            if code and code.strip("0"):
                self.reject(role, code, response.get("err_msg"), response)
            order = str(response.get("ord_no") or "").strip().lstrip("0")
            self.journal.write("ORDER_SUBMIT", role=role, order_no=order, response=response)
            if not order:
                raise RuntimeError("NH REST did not return an order number")
            stage = "overseas_order-history"
            fill = self.broker.wait_fill(order, side=side_name, qty=1, ref_price=price, role=role)
            if fill is None or fill.qty != 1 or not isfinite(fill.price) or fill.price <= 0:
                raise RuntimeError("NH REST did not confirm one filled contract")
            rows = [r for r in self.broker.history()
                    if str(r.get("occurs1_ord_no") or "").strip().lstrip("0") == order]
            confirmed = now()
            times = [at for row in rows if (at := fill_timestamp(row)) is not None and at <= confirmed]
            at = max(times) if times else confirmed
            stage = "overseas_open-interest / overseas_order-history"
            self.assert_position(side if entry else 0)
            self.uncertain = False
            return at, fill.price
        except OrderRejected:
            raise
        except Exception as exc:
            self.uncertain = True
            self.journal.error(stage, exc, role=role, reason=reason, action="자동 주문 잠금. NH 주문·체결·잔고 수동 대사")
            self.journal.write("LOCK_UNKNOWN", api=stage, role=role, reason=reason, message=lock_message(exc),
                               action="자동 주문 잠금. NH 주문·체결·잔고 수동 대사")
            raise TradingLocked(lock_message(exc)) from None

    def reject(self, role, code, message, response):
        self.journal.write("ORDER_REJECT", api="overseas_order", role=role, code=code, message=message,
                           response=response, action="주문 거부 기록. 재주문 없이 운영 중단")
        raise OrderRejected(str(message or code))


def stop_breached(price, stop, side):
    return (price - stop) * side <= 1e-10


def execution_reference(quote, side):
    return quote.ask if side == 1 else quote.bid


async def run_live(root, session, thresholds, exchange, stamp, *, previous=(), risk=RiskSettings()):
    symbol = thresholds.execution_symbol
    journal = RestJournal(symbol, root=root)
    root.mkdir(parents=True, exist_ok=True)
    execution = PaperExecution(symbol, journal)
    journal.write("RUN_START", environment="paper", strategy="C", entry_open=str(ENTRY_OPEN),
                  entry_close=str(ENTRY_CLOSE), force_close=str(risk.force), run_end=str(RUN_END))
    try:
        await asyncio.to_thread(execution.assert_position, 0)
    except Exception as exc:
        journal.error("overseas_open-interest / overseas_order-history", exc, action="운영 시작 중단. 잔고 및 미체결 대사")
        journal.write("LOCK_UNKNOWN", message=lock_message(exc), action="NH 주문·체결·잔고 수동 대사")
        raise
    try:
        code = await asyncio.to_thread(quote_symbol, symbol, exchange)
    except Exception as exc:
        journal.error("overseas_price", exc, action="실시간 종목 코드 확인 실패. 운영 시작 중단")
        raise
    now = datetime.now(KST)
    day = now.date()
    cutoff = datetime.combine(day, RUN_END, KST)
    if now >= cutoff:
        raise ValueError("The KST trading window has already closed")
    costs = Costs(thresholds.fee_per_side, thresholds.slippage_ticks, thresholds.tick_value)
    p = Portfolio("C", thresholds, costs)
    p.entries[day] = sum(event["type"] == "ENTRY" for event in previous)
    p.realized[day] = sum(event["net"] for event in previous if event["type"] == "TRADE")
    def report_retry(exc, delay):
        journal.error("overseas_chart-minute", exc, action=f"{delay}초 뒤 읽기 요청 재시도")

    series = IndicatorSeries()
    try:
        history = await asyncio.to_thread(fetch_bars, thresholds.symbol, exchange, BASE_FIRST, day, stamp, now=now, on_retry=report_retry)
    except Exception as exc:
        journal.error("overseas_chart-minute", exc, action="지표 준비 실패. 운영 시작 중단")
        raise
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

        async def paper_fill(side, ref, *, entry, reason=None):
            nonlocal shutting_down
            order = asyncio.create_task(asyncio.to_thread(
                execution.execute, side, ref, entry=entry, reason=reason, entry_until=min(ENTRY_CLOSE, risk.force)))
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
            fill = await paper_fill(side, ref, entry=False, reason=reason)
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
                    if reason := risk.exit_reason(p, now, stopped):
                        await close(reason, now)
                if task is None and now >= next_rest and now < cutoff:
                    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
                    task = asyncio.create_task(asyncio.to_thread(fetch_bars, thresholds.symbol, exchange,
                                                                   day, day, stamp, now=now, on_retry=report_retry))
                    next_rest = boundary + STEP
                if task and task.done():
                    try:
                        bars = task.result()
                    except Exception as exc:
                        journal.error("overseas_chart-minute", exc, action="확정봉 수신 대기. 다음 조회에서 재확인")
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
                            await close("EXIT_STRATEGY", datetime.now(KST))
                        if shutting_down or p.position or not edges:
                            continue
                        signal = risk.signal(edges[0], row.atr)
                        at = datetime.now(KST)
                        quote = feed.tradable(symbol, at)
                        reason = risk.entry_reason(p, signal, at, quote)
                        if reason:
                            p.hold(signal, reason)
                            journal.write("HOLD", regime=signal.regime, **p.holds[-1])
                            continue
                        price = execution_reference(quote, signal.side)
                        fill = await paper_fill(signal.side, price, entry=True)
                        if fill is None:
                            p.hold(signal, "HOLD_ENTRY_TIME" if datetime.now(KST).time() >= min(ENTRY_CLOSE, risk.force) else "HOLD_NOT_ORDERABLE")
                            journal.write("HOLD", regime=signal.regime, **p.holds[-1])
                            continue
                        p.enter(signal, *fill, price)
                        journal.write("ENTRY", strategy=p.variant, signal_at=signal.at, at=fill[0],
                                      side=signal.side, price=fill[1], rule=signal.rule, regime=signal.regime,
                                      stop=p.position.stop)
                        latest = feed.ticks.get(symbol)
                        stop_price = p.position.stop
                        breached = bool(latest and latest.received >= fill[0]
                                        and stop_breached(latest.price, stop_price, signal.side))
                        feed.stop = (stop_price, signal.side, breached)
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            shutting_down = True
        finally:
            try:
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                if p.position and not await close("EXIT_SHUTDOWN", datetime.now(KST)):
                    journal.write("POSITION_OPEN", strategy=p.variant)
            finally:
                stop.set()
                await asyncio.gather(stream_task, return_exceptions=True)
                handle.flush()
                export(root, [p], rows, thresholds)
                export_spreads(root, root / "quotes.csv")
                journal.write("RUN_END", local_position_open=p.position is not None, locked=execution.uncertain)
        if execution.uncertain:
            raise TradingLocked("Manual reconciliation required")
        try:
            await asyncio.to_thread(execution.assert_position, 0)
        except Exception as exc:
            journal.error("overseas_open-interest / overseas_order-history", exc,
                          action="종료 대사 실패. NH 주문·체결·잔고 수동 대사")
            journal.write("LOCK_UNKNOWN", message=lock_message(exc), action="NH 주문·체결·잔고 수동 대사")
            raise
