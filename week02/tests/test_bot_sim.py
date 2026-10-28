"""main 루프를 가상 시계·가상 시세로 하루 끝까지 돌려 봅니다(DRY_RUN, API 호출 없음)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from bot.broker import Broker
from bot.clock import KST, SimClock, at
from bot.journal import Journal
from bot.main import Bot
from bot.strategy import Bar

DAY = date(2026, 10, 28)


def mk(hhmm: str, close: float, high: float | None = None, low: float | None = None) -> Bar:
    start = at(DAY, hhmm)
    return Bar(start, start + timedelta(minutes=5), close, high or close, low or close, close)


class FakeMarket:
    def __init__(self, bars: list[Bar], price) -> None:
        self.bars = bars
        self.price = price
        self.clock: SimClock | None = None

    def get_bars_5m(self, today, now):
        return [b for b in self.bars if b.end <= now]

    def get_price(self) -> float:
        return self.price(self.clock.now())


class FakeBroker(Broker):
    """DRY_RUN 브로커에서 조회 API 두 개만 가짜로."""

    def pending_count(self) -> int:
        return 0

    def orderable_qty(self, side, price) -> int:
        return 3


def run(tmp_path, bars, price):
    clock = SimClock(datetime(2026, 10, 28, 8, 55, tzinfo=KST))
    market = FakeMarket(bars, price)
    market.clock = clock
    journal = Journal("TEST", clock=clock, root=tmp_path, echo=False)
    bot = Bot(symbol="TEST", market=market, broker=FakeBroker("TEST", journal, clock, dry_run=True),
              journal=journal, clock=clock, environment="test", day=DAY)
    bot.run()
    lines = (tmp_path / "journal_20261028.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def range_bars():
    return [mk("09:00", 100, 105, 95), mk("09:05", 102, 110, 98), mk("09:10", 97, 104, 90), mk("09:15", 100, 103, 96)]


def test_long_entry_then_take_profit(tmp_path):
    bars = range_bars() + [mk("09:20", 111), mk("09:25", 112)]

    def price(now: datetime) -> float:
        return 112 if now < at(DAY, "09:40") else 123  # 진입 112, 익절 112+10틱(1원)=122

    records = run(tmp_path, bars, price)
    kinds = [r["type"] for r in records]
    for kind in ("RUN_START", "RANGE_SET", "BREAKOUT", "CONFIRM_OK", "ORDER_SUBMIT", "FILL", "EXIT_TRIGGER",
                 "TRADE_CLOSED", "DONE", "RUN_END"):
        assert kind in kinds, kind
    closed = next(r for r in records if r["type"] == "TRADE_CLOSED")
    assert closed["reason"] == "TAKE"
    assert closed["entry_price"] == 112 and closed["exit_price"] == 123
    assert closed["ticks"] == 11 and closed["pnl_krw"] == 110
    assert closed["slippage_ticks"] == 1
    assert records[-1]["type"] == "RUN_END" and records[-1]["position"] is None


def test_short_held_until_force_close(tmp_path):
    bars = range_bars() + [mk("09:20", 89), mk("09:25", 88)]
    records = run(tmp_path, bars, lambda now: 88.0)
    closed = next(r for r in records if r["type"] == "TRADE_CLOSED")
    assert closed["side"] == "SHORT" and closed["reason"] == "FORCE"
    trigger = next(r for r in records if r["type"] == "EXIT_TRIGGER")
    assert trigger["ts"].startswith("2026-10-28T15:15")
    assert records[-1]["position"] is None


def test_risk_block_is_recorded(tmp_path):
    bars = range_bars() + [mk("09:20", 111), mk("09:25", 112)]
    # 현재가 1,200,000 → 명목금액 12,000,000 > 10,000,000, 손절 5틱×1000×10=50,000 > 30,000
    records = run(tmp_path, bars, lambda now: 1_200_000.0)
    blocks = {r["check"] for r in records if r["type"] == "RISK_BLOCK"}
    assert blocks == {"1_MAX_LOSS", "2_NOTIONAL"}
    assert not any(r["type"] == "ORDER_SUBMIT" for r in records)
