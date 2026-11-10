from datetime import datetime
from types import SimpleNamespace
import pytest

from spread.strategy import Settings, Quote, KST
from spread.strategy import Signal
from spread.broker import SimBroker, PaperBroker, Uncertain
from spread.broker import PairExecutor
from spread.journal import Journal

T = datetime(2026, 11, 9, 12, tzinfo=KST)
N = Quote("N", T, 1445, 1445.1)
F = Quote("F", T, 1446.7, 1446.8)
S = Signal(T, -1, 1, 0.4, 2, 1.8)


class FaultBroker(SimBroker):
    def __init__(self, journal, fail_role, unknown=False):
        super().__init__(("N", "F"), journal)
        self.fail_role, self.unknown = fail_role, unknown
        self.calls = []

    def execute(self, symbol, side, quote, role):
        self.calls.append(role)
        if role == self.fail_role:
            if self.unknown:
                raise Uncertain("TEST transport timeout")
            return None
        return super().execute(symbol, side, quote, role)


def engine(tmp_path, fail="NONE", unknown=False):
    journal = Journal(tmp_path, "test")
    b = FaultBroker(journal, fail, unknown)
    return PairExecutor(b, "N", "F", journal, Settings(fee_per_leg_side=100)), b


def test_pair_fill_directions_and_flat_at_close(tmp_path):
    e, b = engine(tmp_path)
    assert e.open(S, N, F)
    assert b.positions == {"N": 1, "F": -1}
    assert e.close(N, F, "TEST")
    assert b.positions == {"N": 0, "F": 0}
    assert e.realized == pytest.approx(-2400)  # 두 호가 차이 0.2원 + 4편도 수수료


def test_rejected_first_leg_does_not_submit_second(tmp_path):
    e, b = engine(tmp_path, "ENTRY_NEAR")
    assert not e.open(S, N, F)
    assert b.calls == ["ENTRY_NEAR"]
    assert b.positions == {"N": 0, "F": 0}
    assert e.entries == 1


def test_rejected_second_leg_unwinds_and_halts(tmp_path):
    e, b = engine(tmp_path, "ENTRY_FAR")
    assert not e.open(S, N, F)
    assert b.calls == ["ENTRY_NEAR", "ENTRY_FAR", "UNWIND_NEAR"]
    assert b.positions == {"N": 0, "F": 0}
    assert e.halted
    assert e.realized == pytest.approx(-1200)


def test_unknown_second_leg_does_not_send_blind_unwind(tmp_path):
    e, b = engine(tmp_path, "ENTRY_FAR", unknown=True)
    with pytest.raises(Uncertain):
        e.open(S, N, F)
    assert b.calls == ["ENTRY_NEAR", "ENTRY_FAR"]
    assert b.positions["N"] == 1


def test_partial_pair_exit_retry_does_not_repeat_closed_leg(tmp_path):
    e, b = engine(tmp_path)
    e.open(S, N, F)
    b.fail_role = "EXIT_F"
    assert not e.close(N, F, "FORCE_CLOSE")
    assert b.positions == {"N": 0, "F": -1}
    b.fail_role = "NONE"
    assert e.close(N, F, "FORCE_CLOSE")
    assert b.calls.count("EXIT_N") == 1
    assert b.positions == {"N": 0, "F": 0}


def test_existing_position_blocks_new_orders(tmp_path):
    e, b = engine(tmp_path)
    b.positions["N"] = 1
    with pytest.raises(Uncertain):
        e.open(S, N, F)
    assert not b.calls


def paper(tmp_path):
    cfg = SimpleNamespace(default_environment="paper")
    session = SimpleNamespace(config=cfg, is_replay=False, base_url=lambda env: "https://apidemo.futures.co.kr")
    return PaperBroker(SimpleNamespace(session=session), ("N", "F"), Journal(tmp_path, "test"), timeout=0)


def test_paper_broker_forbids_real_session(tmp_path):
    session = SimpleNamespace(config=SimpleNamespace(default_environment="real"), is_replay=False)
    with pytest.raises(ValueError):
        PaperBroker(SimpleNamespace(session=session), ("N", "F"), Journal(tmp_path, "test"))


def test_transport_timeout_submits_once_and_halts(tmp_path):
    from backend.nh_api.errors import TransportError
    b = paper(tmp_path)
    calls = []
    def order(**kwargs):
        calls.append(kwargs)
        raise TransportError("domestic_order", "timeout")
    b.nh = SimpleNamespace(order=order, MARKET_PRICE="2", FAS="1")
    b.history = lambda **kwargs: []
    b.snapshot = lambda: ({"N": 0, "F": 0}, [])
    with pytest.raises(Uncertain):
        b.execute("N", 1, N, "ENTRY_NEAR")
    assert len(calls) == 1


def test_empty_zero_padded_count_is_valid_but_missing_count_is_not(tmp_path):
    b = paper(tmp_path)
    assert b._pages(lambda **kw: {"nrec": "0000"}, "test") == []
    with pytest.raises(Uncertain):
        b._pages(lambda **kw: {}, "test")


def test_nonzero_count_with_empty_rows_is_not_flat_account(tmp_path):
    b = paper(tmp_path)
    with pytest.raises(Uncertain):
        b._pages(lambda **kw: {"nrec": "0001", "Occurs1": []}, "test")


def test_history_pagination_loop_is_uncertain(tmp_path):
    b = paper(tmp_path)
    with pytest.raises(Uncertain):
        b._pages(lambda **kw: {"Occurs1": [], "nxt_key": "REPEAT"}, "test")


def test_fill_deduplicates_exchange_execution_number(tmp_path):
    b = paper(tmp_path)
    row = dict(occurs1_ord_no="000123", occurs1_sym="N", occurs1_trd_div="1",
               occurs1_exec_qty="1", occurs1_exec_pric="1445.1", occurs1_exch_exec_no="EX1")
    assert b._fill("123", "N", 1, [row, row]).price == 1445.1
    with pytest.raises(Uncertain):
        b._fill("123", "N", 1, [row, {**row, "occurs1_exch_exec_no": "EX2"}])


def test_fill_falls_back_to_nonzero_average_price(tmp_path):
    b = paper(tmp_path)
    row = dict(occurs1_ord_no="123", occurs1_sym="N", occurs1_trd_div="1",
               occurs1_exec_qty="1", occurs1_exec_pric="0", occurs1_avg_exec_pric="1445.1",
               occurs1_exch_exec_no="EX1")
    assert b._fill("123", "N", 1, [row]).price == 1445.1


def test_cancel_race_detects_fill_instead_of_assuming_cancelled(tmp_path, monkeypatch):
    import spread.broker as module
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    b = paper(tmp_path)
    cancelled = []
    b.nh = SimpleNamespace(order=lambda **kw: {"ord_no": "000123"},
                           cancel=lambda *a, **kw: cancelled.append(kw) or {}, MARKET_PRICE="2", FAS="1")
    row = dict(occurs1_ord_no="123", occurs1_sym="N", occurs1_trd_div="1",
               occurs1_exec_qty="1", occurs1_exec_pric="1445.1", occurs1_exch_exec_no="EX1")
    b.history = lambda **kw: [row] if cancelled else []
    result = b.execute("N", 1, N, "ENTRY_NEAR")
    assert result.price == 1445.1
    assert cancelled[0]["org_ord_no"] == "0000000123"
