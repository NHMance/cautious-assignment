"""주문 한 건(접수 → 체결 → 잔고)이 어긋날 때와 예상 못한 예외 — 서버·인증 없음."""
from __future__ import annotations

from datetime import date, datetime, time

from bot.broker import Fill, Position  # week02
from bot.clock import KST, SimClock
from backend.nh_api.errors import BusinessError
from trader import broker as broker_mod
from trader import settings
from trader.bot import TraderBot, count_entries, pnl_for_limit
from trader.journal import DayJournal
from trader.strategy import LONG, SHORT, Session, Trade, levels

DAY = date(2026, 11, 3)
ATR = 0.0004


def journal(tmp_path, clock):
    return DayJournal("MKUX26", DAY, clock=clock, root=tmp_path, echo=False)


class FakeMarket:
    last_price_source = "rest"

    def __init__(self, price=0.7500):
        self.price = price
        self.fail_bars = 0

    def get_bars(self, session, now):
        if self.fail_bars:
            self.fail_bars -= 1
            raise RuntimeError("봉 조회 중 예상 못한 오류")
        return []

    def get_price(self):
        return self.price


class FakeBroker:
    dry_run = False

    def __init__(self):
        self.held: Position | None = None
        self.pending = 0
        self.submitted: list[tuple[str, int]] = []
        self.cancel_calls = 0

    def position(self):
        return self.held

    def pending_count(self):
        return self.pending

    def orderable_qty(self, side, price):
        return 1

    def submit(self, side, qty, *, role):
        self.submitted.append((side, qty))
        return f"N{len(self.submitted)}"

    def wait_fill(self, order_no, *, side, qty, ref_price, role):
        self.held = None
        return Fill(order_no, ref_price, qty)

    def cancel_pending(self):
        self.cancel_calls += 1
        self.pending = 0
        return 1


def make_bot(tmp_path, *, start=datetime(2026, 11, 3, 10, 0, tzinfo=KST)):
    clock = SimClock(start)
    sess = Session.of(DAY, time(9, 0), time(17, 0))
    bot = TraderBot(symbol="MKUX26", market=FakeMarket(), broker=FakeBroker(), journal=journal(tmp_path, clock),
                    clock=clock, session=sess, environment="test")
    return bot


def long_trade(entry=0.7500):
    return Trade.from_levels(levels(LONG, entry, ATR), 1, "N0", "2026-11-03T10:00:00+09:00")


def rows(bot, type_=None, code=None):
    out = bot.journal.today()
    if type_:
        out = [r for r in out if r["type"] == type_]
    if code:
        out = [r for r in out if r.get("code") == code]
    return out


# ── 브로커: 응답을 모르는 주문은 다시 보내지 않음 ─────────────────

def test_submit_unexpected_exception_looks_up_order_instead_of_resending(tmp_path, monkeypatch):
    calls = {"order": 0, "history": 0}
    new_row = {"occurs1_ord_no": "0000000777", "occurs1_sym": "MKUX26", "occurs1_trd_tp": "1", "occurs1_ord_qty": "1"}

    def order(*a, **k):
        calls["order"] += 1
        raise ValueError("응답 JSON 을 읽지 못함")  # 서버에는 접수됐을 수 있음

    def history(**_):
        calls["history"] += 1
        return {"Occurs1": [] if calls["history"] == 1 else [new_row]}

    monkeypatch.setattr(broker_mod.api, "order", order)
    monkeypatch.setattr(broker_mod.api, "order_history", history)
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    j = journal(tmp_path, clock)
    assert broker_mod.OverseasBroker("MKUX26", j, clock).submit(LONG, 1, role="ENTRY") == "777"
    assert calls["order"] == 1
    assert any(r["type"] == "ORDER_SUBMIT" and r.get("recovered_from_timeout") for r in j.today())


def test_submit_unknown_and_not_found_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_mod.api, "order", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr(broker_mod.api, "order_history", lambda **_: {"Occurs1": []})
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    j = journal(tmp_path, clock)
    assert broker_mod.OverseasBroker("MKUX26", j, clock).submit(LONG, 1, role="EXIT") is None
    assert any(r.get("code") == "ORDER_UNKNOWN" for r in j.today())


def test_submit_server_error_is_definite_failure(tmp_path, monkeypatch):
    def order(*a, **k):
        raise BusinessError("overseas_order", "E1", "증거금 부족", {})

    monkeypatch.setattr(broker_mod.api, "order", order)
    monkeypatch.setattr(broker_mod.api, "order_history", lambda **_: {"Occurs1": []})
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    j = journal(tmp_path, clock)
    assert broker_mod.OverseasBroker("MKUX26", j, clock).submit(LONG, 1, role="ENTRY") is None
    assert not any(r.get("code") == "ORDER_UNKNOWN" for r in j.today())


def test_wait_fill_survives_unexpected_history_error(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_mod.api, "order_history", lambda **_: (_ for _ in ()).throw(KeyError("Occurs1")))
    monkeypatch.setattr(broker_mod.api, "order", lambda *a, **k: {"err_cd": ""})
    clock = SimClock(datetime(2026, 11, 3, 10, tzinfo=KST))
    j = journal(tmp_path, clock)
    b = broker_mod.OverseasBroker("MKUX26", j, clock)
    assert b.wait_fill("123", side=LONG, qty=1, ref_price=0.75, role="ENTRY") is None
    assert any(r.get("code") == "FILL_TIMEOUT" for r in j.today())


# ── 봇: 잔고 맞춤 ─────────────────────────────────────────

def test_orphan_position_is_adopted_and_counted(tmp_path):
    bot = make_bot(tmp_path)
    bot.broker.held = Position(SHORT, 1, 0.7510)
    bot.on_bar_tick(bot.clock.now())          # ATR 을 아직 몰라 손절·익절은 다음 봉에서
    assert bot.strategy.entries == 1 and bot.strategy.position == SHORT
    assert rows(bot, code="ORPHAN_POSITION")
    bot._last_atr = ATR
    bot.on_bar_tick(bot.clock.now())
    assert bot.trade is not None and bot.trade.side == SHORT and bot.trade.entry_price == 0.7510
    opened = rows(bot, "ENTRY_OPEN")[-1]
    assert opened["orphan"] and opened["entry_counted"]
    assert len(rows(bot, code="ORPHAN_POSITION")) == 1   # 한 번만 거둡니다
    assert count_entries(bot.journal.today()) == 1


# ── 봇: 중복 청산 방지 ────────────────────────────────────

def test_exit_waits_for_pending_then_cancels(tmp_path):
    bot = make_bot(tmp_path)
    bot.trade = long_trade()
    bot.strategy.on_entry_filled(LONG)
    bot.broker.held = Position(LONG, 1, 0.7500)
    bot.broker.pending = 1
    for _ in range(settings.EXIT_PENDING_WAIT_ATTEMPTS):
        bot.exit("STOP", 0.7490)
    assert bot.broker.submitted == [] and bot.broker.cancel_calls == 0
    bot.exit("STOP", 0.7490)                  # 기다려도 남아 있음 → 취소
    assert bot.broker.cancel_calls == 1 and bot.broker.submitted == []
    bot.exit("STOP", 0.7490)                  # 미체결 없어짐 → 청산 주문
    assert bot.broker.submitted == [(SHORT, 1)]
    assert bot.trade is None and bot.strategy.position is None


def test_exit_stuck_warning(tmp_path):
    bot = make_bot(tmp_path)
    bot.trade = long_trade()
    bot.broker.held = Position(LONG, 1, 0.7500)
    bot.broker.wait_fill = lambda *a, **k: None  # 체결 확인이 계속 실패
    for _ in range(settings.EXIT_STUCK_ATTEMPTS):
        bot.exit("STOP", 0.7490)
    assert len(rows(bot, code="EXIT_STUCK")) == 1
    assert bot.trade is not None              # 손을 놓지 않습니다


# ── 봇: 청산가를 모르는 청산은 손절로 보고 한도에 반영 ─────────

def test_already_flat_counts_stop_loss_toward_daily_limit(tmp_path):
    bot = make_bot(tmp_path)
    trade = long_trade()
    bot.trade = trade
    bot.strategy.on_entry_filled(LONG)
    bot.broker.held = None
    bot.exit("STOP", None)
    closed = rows(bot, "TRADE_CLOSED")[-1]
    assert closed["pnl_usd"] is None
    assert closed["pnl_for_limit_usd"] == trade.pnl_usd(trade.stop) < 0
    assert bot.strategy.realized_pnl == closed["pnl_for_limit_usd"]
    assert bot.trade is None and bot.strategy.position is None


def test_closed_state_resets_even_if_journal_write_fails(tmp_path):
    bot = make_bot(tmp_path)
    bot.trade = long_trade()
    bot.strategy.on_entry_filled(LONG)
    original = bot.journal.write

    def failing(type_, **fields):
        if type_ == "TRADE_CLOSED":
            raise OSError("디스크 가득 참")
        return original(type_, **fields)

    bot.journal.write = failing
    try:
        bot._closed(bot.trade, "TAKE", 0.7510, "N9")
    except OSError:
        pass
    assert bot.trade is None and bot.strategy.position is None
    assert bot.strategy.realized_pnl > 0


# ── 봇: 루프 격리 ─────────────────────────────────────────

def fail_strategy(bot, times):
    """API 가 아닌 곳(전략 계산)에서 예상 못한 예외 — on_bar_tick 안에서 잡히지 않는 자리."""
    original = bot.strategy.on_bars
    left = {"n": times}

    def on_bars(bars, now):
        if left["n"] > 0:
            left["n"] -= 1
            raise RuntimeError("지표 계산 중 예상 못한 오류")
        return original(bars, now)

    bot.strategy.on_bars = on_bars


def test_loop_error_is_recorded_and_run_continues(tmp_path):
    bot = make_bot(tmp_path, start=datetime(2026, 11, 3, 16, 50, tzinfo=KST))
    fail_strategy(bot, 2)
    assert bot.run() == "DEADLINE"
    assert len(rows(bot, code="LOOP_ERROR")) == 2
    assert rows(bot, "RUN_END")[-1]["stop_reason"] == "DEADLINE"


def test_too_many_consecutive_loop_errors_stop_the_bot(tmp_path):
    bot = make_bot(tmp_path, start=datetime(2026, 11, 3, 10, 0, tzinfo=KST))
    fail_strategy(bot, 10_000)
    bot._next_fetch = lambda now: now          # 매 바퀴 봉을 조회하게
    try:
        bot.run()
    except RuntimeError:
        pass
    else:
        raise AssertionError("연속 오류에서 멈춰야 합니다")
    assert len(rows(bot, code="LOOP_ERROR")) == settings.MAX_CONSECUTIVE_LOOP_ERRORS
    assert rows(bot, "RUN_END")[-1]["stop_reason"] == "CRASH"


# ── 복구 ──────────────────────────────────────────────────

def test_count_entries_and_pnl_for_limit():
    new = [{"type": "ENTRY_OPEN", "entry_counted": True}, {"type": "ENTRY_OPEN", "entry_counted": False},
           {"type": "ENTRY_OPEN", "entry_counted": True, "orphan": True}]
    assert count_entries(new) == 2
    old = [{"type": "FILL", "role": "ENTRY", "order_no": "1", "partial": True},
           {"type": "FILL", "role": "ENTRY", "order_no": "2"}, {"type": "FILL", "role": "EXIT", "order_no": "3"}]
    assert count_entries(old) == 2           # 부분 체결도 진입으로 셈
    assert pnl_for_limit({"pnl_usd": -5.0}) == -5.0
    assert pnl_for_limit({"pnl_usd": None, "pnl_for_limit_usd": -7.5}) == -7.5
