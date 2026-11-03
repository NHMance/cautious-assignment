"""B 재계산 — 가정 체결 규칙과 A 공유, 그리고 봇 테스트 A→B 전체 흐름."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest

import trader.journal as tj
from trader import settings
from trader.recompute import Recompute, RecordClock
from trader.strategy import LONG, Bar, Session, Strategy, Trade

DAY = date(2026, 11, 2)


def make_engine(tmp_path, trade: Trade) -> Recompute:
    sess = Session.of(DAY, time(9, 0), time(17, 0))
    clock = RecordClock(sess.open)
    journal = tj.DayJournal(settings.SYMBOL, DAY, clock=clock, root=tmp_path, echo=False)
    engine = Recompute(journal, clock, source=tmp_path / "a.jsonl")
    engine.session = sess
    engine.strategy = Strategy("B", sess)
    engine.strategy.on_entry_filled(trade.side)
    engine.trade, engine.trade_source = trade, "assumed"
    return engine


def long_trade(entry_ts: datetime) -> Trade:
    return Trade(LONG, 1, 0.7500, 0.7495, 0.7507, 5, 7, 0.00037, None, entry_ts.isoformat())


def bar(start: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(start, start + timedelta(minutes=5), o, h, low, c)


T0 = Session.of(DAY, time(9, 0), time(17, 0)).open + timedelta(hours=1)


def test_both_hit_in_one_bar_counts_as_stop(tmp_path):
    e = make_engine(tmp_path, long_trade(T0))
    e._check_assumed_exit(bar(T0, 0.7500, 0.7510, 0.7490, 0.7502))
    assert e.closed[-1]["reason"] == "STOP" and e.closed[-1]["exit_price"] == 0.7495
    assert e.closed[-1]["pnl_usd"] == pytest.approx(-12.5)


def test_gap_through_stop_fills_at_open(tmp_path):
    e = make_engine(tmp_path, long_trade(T0))
    e._check_assumed_exit(bar(T0, 0.7490, 0.7492, 0.7488, 0.7491))
    assert e.closed[-1]["reason"] == "STOP" and e.closed[-1]["exit_price"] == 0.7490   # 손절가보다 불리


def test_take_profit(tmp_path):
    e = make_engine(tmp_path, long_trade(T0))
    e._check_assumed_exit(bar(T0, 0.7501, 0.7508, 0.7499, 0.7506))
    assert e.closed[-1]["reason"] == "TAKE" and e.closed[-1]["exit_price"] == 0.7507


def test_force_close_at_30_min_before_close(tmp_path):
    e = make_engine(tmp_path, long_trade(T0))
    force = e.session.force_close
    e._check_assumed_exit(bar(force - timedelta(minutes=10), 0.7500, 0.7501, 0.7499, 0.7500))
    assert not e.closed
    e._check_assumed_exit(bar(force - timedelta(minutes=5), 0.7500, 0.7502, 0.7499, 0.7501))
    assert e.closed[-1]["reason"] == "FORCE" and e.closed[-1]["exit_price"] == 0.7501


def test_bot_test_a_then_b(tmp_path, monkeypatch):
    """합성 봉으로 A(주문 없음) → B 재계산. 공유 거래는 A 와 손익이 같아야 합니다."""
    monkeypatch.setattr(tj, "RESULTS_DIR", tmp_path)
    from trader.main import main

    assert main(["--strategy", "AB", "--bot-test"]) == 0
    read = lambda v: [json.loads(x) for x in (tmp_path / "bot-test" / v / "journal_20261102.jsonl").read_text().splitlines()]
    a, b = read("A"), read("B")
    assert a[-1]["type"] == "RUN_END" and a[-1]["stop_reason"] == "DEADLINE"
    assert b[-1]["type"] == "RUN_END"
    a_closed = {r["entry_order"]: r for r in a if r["type"] == "TRADE_CLOSED"}
    b_closed = [r for r in b if r["type"] == "TRADE_CLOSED"]
    assert len(a_closed) <= settings.MAX_ENTRIES_PER_DAY and len(b_closed) <= settings.MAX_ENTRIES_PER_DAY
    for r in b_closed:
        if r["fill_source"] == "A":
            assert r["pnl_usd"] == a_closed[r["entry_order"]]["pnl_usd"]
    # 손절·익절가는 진입가 쪽으로 맞춰져 있고 손절 거리는 1틱 이상
    for r in [*a_closed.values(), *b_closed]:
        sign = 1 if r["side"] == LONG else -1
        assert (r["entry_price"] - r["stop"]) * sign >= settings.TICK - 1e-12
        assert r["stop_ticks"] == int(settings.STOP_ATR * r["atr"] / settings.TICK + 1e-9)
    # B 는 주문을 내지 않습니다
    assert not any(r["type"] in ("ORDER_SUBMIT", "FILL") for r in b)
