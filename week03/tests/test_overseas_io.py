"""해외 주문·잔고·시세 응답 해석 — API 함수를 가짜로 바꿔 점검합니다(서버·인증 없음).

필드 이름은 week02 backend 해외 파서에서 옮긴 것입니다. 분봉 날짜·시각 칸은 실서버 응답으로 아직 확정 전이라
후보 이름 둘 다 읽히는지 봅니다.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from bot.clock import KST, SimClock
from trader import broker as broker_mod
from trader import market as market_mod
from trader import recompute
from trader.journal import DayJournal
from trader.strategy import LONG, SHORT


def journal(tmp_path, clock):
    return DayJournal("MKUX26", date(2026, 11, 3), clock=clock, root=tmp_path, echo=False)


def test_position_nets_long_and_short_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_mod.api, "open_interest", lambda **_: {"Occurs1": [
        {"occurs1_sym": "MKUX26", "occurs1_trd_tp": "1", "occurs1_opint_qty": "2", "occurs1_avg_pric": "0.7500"},
        {"occurs1_sym": "MKUX26", "occurs1_trd_tp": "2", "occurs1_opint_qty": "1", "occurs1_avg_pric": "0.7510"},
        {"occurs1_sym": "6AZ26", "occurs1_trd_tp": "1", "occurs1_opint_qty": "5", "occurs1_avg_pric": "0.66"},
    ]})
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    pos = broker_mod.OverseasBroker("MKUX26", journal(tmp_path, clock), clock).position()
    assert pos.side == LONG and pos.qty == 1


def test_submit_rejected_by_err_cd(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_mod.api, "order_history", lambda **_: {"Occurs1": []})
    monkeypatch.setattr(broker_mod.api, "order", lambda *a, **k: {"err_cd": "E1234", "err_msg": "증거금 부족", "ord_no": ""})
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    j = journal(tmp_path, clock)
    assert broker_mod.OverseasBroker("MKUX26", j, clock).submit(LONG, 1, role="ENTRY") is None
    assert any(r["type"] == "ERROR" and r["code"] == "E1234" for r in j.today())


def test_submit_and_fill(tmp_path, monkeypatch):
    sent = {}

    def order(sym, **kw):
        sent.update(kw, sym=sym)
        return {"err_cd": "", "ord_no": "0000012345"}

    rows = [{"occurs1_ord_no": "0000012345", "occurs1_sym": "MKUX26", "occurs1_trd_tp": "2", "occurs1_ord_qty": "1",
             "occurs1_exec_qty": "1", "occurs1_exec_pric": "0.7532", "occurs1_ord_sts": "6"}]
    monkeypatch.setattr(broker_mod.api, "order", order)
    monkeypatch.setattr(broker_mod.api, "order_history", lambda **_: {"Occurs1": rows})
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    b = broker_mod.OverseasBroker("MKUX26", journal(tmp_path, clock), clock)
    no = b.submit(SHORT, 1, role="ENTRY")
    assert no == "12345"
    assert sent["trd_div_cd"] == "2" and sent["glb_ord_tp_cd"] == "1" and sent["qt_div_cd"] == "1"
    fill = b.wait_fill(no, side=SHORT, qty=1, ref_price=0.7533, role="ENTRY")
    assert fill.price == 0.7532 and fill.qty == 1


def test_parse_bars_reads_newest_first_and_either_time_field():
    raw = {"Occurs1": [
        {"occurs1_ref_dt": "20261103", "occurs1_exch_tm": "091000", "occurs1_open_pric": "0.7501",
         "occurs1_high_pric": "0.7503", "occurs1_low_pric": "0.7500", "occurs1_close_pric": "0.7502"},
        {"occurs1_biz_dt": "20261103", "occurs1_trd_tm": "09:05:00", "occurs1_open_pric": "0.7500",
         "occurs1_high_pric": "0.7502", "occurs1_low_pric": "0.7499", "occurs1_close_pric": "0.7501"},
    ]}
    bars = market_mod.parse_bars(raw)
    assert [b.start.strftime("%H:%M") for b in bars] == ["09:05", "09:10"]
    assert bars[0].end.strftime("%H:%M") == "09:10"


def test_session_hours_from_market_operations(monkeypatch):
    monkeypatch.setattr(market_mod.api, "market_operations", lambda **_: {"Occurs1": [
        {"occurs1_exch_cd": "SGX", "occurs1_prd_cd": "MKU", "occurs1_trd_tm_inf": "08:25~18:15"}]})
    monkeypatch.setattr(market_mod, "save_sample", lambda *a, **k: None)
    opened, closed = market_mod.session_hours()
    assert (opened.hour, opened.minute, closed.hour, closed.minute) == (8, 25, 18, 15)


def test_b_follow_mode_stops_at_a_run_end(tmp_path):
    src = tmp_path / "A" / "journal_20261103.jsonl"
    src.parent.mkdir()
    lines = [
        {"ts": "2026-11-03T08:20:00+09:00", "type": "RUN_START", "environment": "paper", "dry_run": True,
         "session": {"open": "2026-11-03T08:30:00+09:00", "close": "2026-11-03T17:00:00+09:00"}},
        {"ts": "2026-11-03T17:00:00+09:00", "type": "RUN_END", "stop_reason": "DEADLINE"},
    ]
    src.write_text("".join(json.dumps(x) + "\n" for x in lines))
    engine = recompute.run(src, tmp_path / "B", follow=True)
    assert engine.finished
