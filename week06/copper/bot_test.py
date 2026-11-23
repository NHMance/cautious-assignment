"""재현 가능한 R1–R6 시나리오와 모의 REST 계좌."""

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from .live import PaperExecution, Quote, RestJournal, RiskSettings, stop_breached
from .market import api
from .replay import Portfolio
from .strategy import KST, TICK, Costs, Signal, Thresholds
from bot.broker import Fill, Position
from bot.clock import SimClock
from trader.strategy import LONG, SHORT

EXPECTED = {
    "R1": "EXIT_STOP", "R2": "HOLD_BUDGET", "R3": "HOLD_DAILY_LOSS",
    "R4": "HOLD_HIGH_VOL", "R5": "EXIT_TIME", "R6": "EXIT_FORCE",
}


class RestAccount:
    symbol = "HGH27"

    def __init__(self):
        self.actual = None
        self.price = 5.0
        self.orders = []

    def pending_count(self):
        return 0

    def position(self):
        return self.actual

    def orderable_qty(self, side, price):
        return 1

    def order(self, symbol, **request):
        if symbol != self.symbol or request["ord_qty"] != "1" or request["glb_ord_tp_cd"] != api.MARKET_PRICE:
            raise AssertionError("Expected a one-contract HGH27 market order")
        self.orders.append(request["trd_div_cd"])
        return {"ord_no": str(len(self.orders))}

    def wait_fill(self, order, *, side, qty, ref_price, role):
        if role == "EXIT":
            if not self.actual or self.actual.side == side:
                raise AssertionError("Exit must offset the open position")
            self.actual = None
        else:
            self.actual = Position(side, qty, self.price)
        return Fill(order, self.price, qty)

    def history(self):
        return []


def scenario(root, rule, start=None):
    start = start or datetime(2026, 11, 23, 17, 0, tzinfo=KST)
    clock = SimClock(start)
    journal = RestJournal("HGH27", root=root, clock=clock, echo=False)
    journal.write("RUN_START", environment="bot-test", scenario=rule)
    risk = RiskSettings()
    thresholds = Thresholds("HGH27", "HGH27", 0.7, 0.3, 1.5, {}, 200, 600, 0, 1)
    if rule == "R1":
        risk = replace(risk, stop_minimum=1, stop_multiple=0)
    elif rule == "R2":
        thresholds = replace(thresholds, trade_loss_budget=1)
    elif rule == "R3":
        thresholds = replace(thresholds, daily_loss_limit=10)
    elif rule == "R4":
        thresholds = replace(thresholds, rv_hi=0.5)
    elif rule == "R5":
        risk = replace(risk, max_hold=timedelta(minutes=5))
    elif rule == "R6":
        risk = replace(risk, force=(start + timedelta(minutes=10)).time())
    p = Portfolio("C", thresholds, Costs(0, 1, 12.5))
    account = RestAccount()
    execution = PaperExecution("HGH27", journal)
    execution.broker = account

    def check():
        at = clock.now()
        from .strategy import Bar, Indicators, SignalEdges, time_bucket

        bar = Bar(at - timedelta(minutes=5), 5, 5, 5, 5, 1)
        test_thresholds = replace(thresholds, atr_medians={time_bucket(bar): 0.002})
        row = Indicators(5, 4.9, 0.002, 0.8, 0)
        edge, = SignalEdges("C").push(bar, row, test_thresholds)
        signal = risk.signal(edge, row.atr)
        reason = risk.entry_reason(p, signal, at, Quote(at, 5.0 - TICK, 5.0))
        if reason:
            journal.write("HOLD", reason=reason, regime=signal.regime)
        return signal, reason

    with patch.object(api, "order", account.order):
        signal, reason = check()
        if not reason:
            p.enter(signal, *execution.execute(1, 5.0, entry=True, clock=clock.now))
            journal.write("ENTRY", price=p.position.price, side=signal.side)
            if rule in ("R1", "R3"):
                account.price = p.position.stop
            elif rule == "R5":
                clock.sleep(5 * 60 - 1)
                if risk.exit_reason(p, clock.now(), False):
                    raise AssertionError("Exited before five minutes")
                clock.sleep(1)
            elif rule == "R6":
                clock.sleep(10 * 60 - 1)
                if risk.exit_reason(p, clock.now(), False):
                    raise AssertionError("Exited before the force-close time")
                clock.sleep(1)
            stopped = stop_breached(account.price, p.position.stop, signal.side)
            reason = risk.exit_reason(p, clock.now(), stopped)
            if reason:
                p.exit(*execution.execute(-1, account.price, entry=False, reason=reason, clock=clock.now), reason)
                journal.write("TRADE", **p.trades[-1])
            if rule == "R3":
                _, reason = check()
        execution.assert_position(0)

    expected_orders = [] if rule in ("R2", "R4") else ["1", "2"]
    if reason != EXPECTED[rule] or account.orders != expected_orders or p.position is not None:
        raise AssertionError(f"{rule}: {reason=}, {account.orders=}")
    journal.write("BOT_TEST_PASS", rule=rule, reason=reason, orders=len(account.orders), position_qty=0, pending=0)
    return reason


def run_scenarios(root):
    run = root / datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f")
    for rule in EXPECTED:
        reason = scenario(run / rule, rule)
        print(f"{rule} {reason} PASS")
    print(run)
