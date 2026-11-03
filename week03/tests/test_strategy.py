"""전략 규칙 (이슈 #1~#3) — 순수 로직만. 서버·인증 없음."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from trader import settings
from trader.indicators import atr_base
from trader.strategy import LONG, SHORT, Action, Bar, Session, Strategy, levels

KST = datetime(2026, 11, 2).astimezone().tzinfo
DAY = date(2026, 11, 2)


def session() -> Session:
    return Session.of(DAY, time(9, 0), time(17, 0))


# ── 운영 시간 ───────────────────────────────────────────────

def test_session_windows():
    s = session()
    assert (s.entry_cutoff.hour, s.entry_cutoff.minute) == (16, 15)   # 마감 45분 전
    assert (s.force_close.hour, s.force_close.minute) == (16, 30)     # 마감 30분 전
    assert s.close.hour == 17                                          # 종료 = 장 마감


def test_session_crossing_midnight():
    s = Session.of(DAY, time(18, 0), time(2, 0))
    assert s.close - s.open == timedelta(hours=8)


# ── 지표 ────────────────────────────────────────────────────

def test_atr_base_excludes_current_bar():
    atrs = [None, 1.0, 2.0, 3.0, 100.0]
    base = atr_base(atrs, 3)
    assert base[3] is None            # 직전 3개 중 None 이 있음
    assert base[4] == pytest.approx(2.0)  # (1+2+3)/3 — 현재 봉 100 은 넣지 않음


# ── 손절·익절 틱 맞춤 ──────────────────────────────────────

def test_levels_long_rounds_toward_entry():
    # ATR 3.7틱 → 손절 5.55틱→5틱(손절가 올림), 익절 7.4틱→7틱(익절가 내림)
    lv = levels(LONG, 0.7500, 3.7 * settings.TICK)
    assert (lv.stop_ticks, lv.take_ticks) == (5, 7)
    assert lv.stop == pytest.approx(0.7495) and lv.take == pytest.approx(0.7507)


def test_levels_short_rounds_toward_entry():
    lv = levels(SHORT, 0.7500, 3.7 * settings.TICK)
    assert lv.stop == pytest.approx(0.7505) and lv.take == pytest.approx(0.7493)


def test_levels_exact_multiple_is_not_cut():
    lv = levels(LONG, 0.7500, 2.0 * settings.TICK)  # 1.5×2 = 3틱 딱 맞음
    assert lv.stop_ticks == 3


def test_stop_below_one_tick_returns_none():
    assert levels(LONG, 0.7500, 0.6 * settings.TICK) is None   # 1.5×0.6 = 0.9틱


# ── 신호·상태 ───────────────────────────────────────────────

def bars_from(closes: list[float], start: datetime, spread: float = 2) -> list[Bar]:
    out = []
    for i, c in enumerate(closes):
        t = start + timedelta(minutes=5 * i)
        prev = closes[i - 1] if i else c
        out.append(Bar(t, t + timedelta(minutes=5), prev, max(prev, c) + spread * settings.TICK,
                       min(prev, c) - spread * settings.TICK, c))
    return out


def flat_then(path: list[float], n_flat: int = 60, base: float = 0.75) -> list[float]:
    """처음엔 지그재그로 지표를 채우고, 뒤에 원하는 움직임을 붙입니다."""
    zig = [base + (-settings.TICK if i % 2 else settings.TICK) for i in range(n_flat)]  # 마지막이 아래쪽 → 장 시작 봉에서 상향 교차
    return zig + path


def feed_until_signal(strategy: Strategy, bars: list[Bar]) -> tuple[Action, list[dict], int]:
    """봉을 하나씩 확정시키며 첫 행동이 나오는 곳을 찾습니다."""
    events: list[dict] = []
    for i in range(1, len(bars) + 1):
        d = strategy.on_bars(bars[:i], bars[i - 1].end)
        events += d.events
        if d.action is not Action.NONE:
            return d.action, events, i
    return Action.NONE, events, len(bars)


def rising_after(n: int = 15) -> list[float]:
    return [0.75 + 0.0003 * (k + 1) for k in range(n)]


def test_a_enters_on_up_cross_inside_window():
    s = session()
    bars = bars_from(flat_then(rising_after()), s.open - timedelta(minutes=5 * 60))
    action, _, _ = feed_until_signal(Strategy("A", s), bars)
    assert action is Action.ENTER_LONG


def test_cross_before_open_is_not_entered():
    s = session()
    bars = bars_from(flat_then(rising_after(), n_flat=40), s.open - timedelta(minutes=5 * 70))
    action, events, _ = feed_until_signal(Strategy("A", s), bars)
    assert action is Action.NONE


def test_reverse_cross_exits_without_reversal_and_reentry_needs_new_cross():
    s = session()
    up, down = rising_after(), [0.7545 - 0.0003 * (k + 1) for k in range(15)]
    closes = flat_then(up + down + [down[-1] + 0.0003 * (k + 1) for k in range(15)])
    bars = bars_from(closes, s.open - timedelta(minutes=5 * 60))
    st = Strategy("A", s)
    actions = []
    for i in range(1, len(bars) + 1):
        d = st.on_bars(bars[:i], bars[i - 1].end)
        if d.action is Action.ENTER_LONG or d.action is Action.ENTER_SHORT:
            st.on_entry_filled(LONG if d.action is Action.ENTER_LONG else SHORT)
        elif d.action is Action.EXIT:
            st.on_closed()
        if d.action is not Action.NONE:
            actions.append(d.action)
    # 상승 교차 진입 → 하락 교차는 청산만(매도 진입 없음) → 다시 상승 교차에서만 재진입
    assert actions[:3] == [Action.ENTER_LONG, Action.EXIT, Action.ENTER_LONG]


def test_max_entries_per_day():
    s = session()
    st = Strategy("A", s, entries_done=settings.MAX_ENTRIES_PER_DAY)
    bars = bars_from(flat_then(rising_after()), s.open - timedelta(minutes=5 * 60))
    action, events, _ = feed_until_signal(st, bars)
    assert action is Action.NONE
    assert any(e["type"] == "SKIP" and e["reason"] == "MAX_ENTRIES" for e in events)


def test_b_blocks_when_atr_ratio_out_of_band():
    s = session()
    # 교차 봉의 범위를 크게 키워 ATR 이 직전 평균의 1.5배를 넘게 만듭니다.
    closes = flat_then(rising_after())
    bars = bars_from(closes, s.open - timedelta(minutes=5 * 60))
    a_action, _, at = feed_until_signal(Strategy("A", s), bars)
    assert a_action is Action.ENTER_LONG
    sig = bars[at - 1]
    bars[at - 1] = Bar(sig.start, sig.end, sig.open, sig.high + 40 * settings.TICK, sig.low - 40 * settings.TICK, sig.close)
    b_action, events, _ = feed_until_signal(Strategy("B", s), bars[:at])
    assert b_action is Action.NONE
    assert any(e["type"] == "ATR_FILTER_BLOCK" and e["atr_ratio"] > settings.ATR_BAND[1] for e in events)


def test_b_enters_same_signal_when_in_band():
    s = session()
    bars = bars_from(flat_then(rising_after()), s.open - timedelta(minutes=5 * 60), spread=2)
    a = feed_until_signal(Strategy("A", s), bars)
    b = feed_until_signal(Strategy("B", s), bars)
    assert a[0] is b[0] is Action.ENTER_LONG and a[2] == b[2]   # 같은 봉에서 같은 방향


def test_night_session_counts_cutoffs_from_trade_end():
    """운영 20:30~08:30, 실제 거래 마감 06:15 → 진입 마감 05:30, 강제 청산 05:45 (다음 날)."""
    s = Session.of(DAY, time(20, 30), time(8, 30), time(6, 15))
    nxt = DAY + timedelta(days=1)
    assert s.open == datetime.combine(DAY, time(20, 30), tzinfo=KST)
    assert s.close == datetime.combine(nxt, time(8, 30), tzinfo=KST)
    assert s.trade_end == datetime.combine(nxt, time(6, 15), tzinfo=KST)
    assert s.entry_cutoff == datetime.combine(nxt, time(5, 30), tzinfo=KST)
    assert s.force_close == datetime.combine(nxt, time(5, 45), tzinfo=KST)
    assert Session.from_dict(s.to_dict()) == s


def test_trade_end_outside_session_is_ignored():
    s = Session.of(DAY, time(8, 25), time(20, 35), time(6, 15))
    assert s.trade_end is None
    assert s.force_close == datetime.combine(DAY, time(20, 5), tzinfo=KST)


def test_default_night_session_hours():
    """기본 운영 20:50~06:15 — 마감이 곧 실제 거래 마감, 진입 마감 05:30, 강제 청산 05:45."""
    from trader import settings
    s = Session.of(DAY, time.fromisoformat(settings.SESSION_OPEN), time.fromisoformat(settings.SESSION_CLOSE),
                   time.fromisoformat(settings.TRADE_END))
    nxt = DAY + timedelta(days=1)
    assert s.open == datetime.combine(DAY, time(20, 50), tzinfo=KST)
    assert s.close == s.trade_end == datetime.combine(nxt, time(6, 15), tzinfo=KST)
    assert s.entry_cutoff == datetime.combine(nxt, time(5, 30), tzinfo=KST)
    assert s.force_close == datetime.combine(nxt, time(5, 45), tzinfo=KST)


def test_daily_loss_limit_blocks_new_entry():
    s = session()
    st = Strategy("A", s, realized_pnl=-settings.DAILY_LOSS_LIMIT_USD)   # 정확히 한도 도달
    bars = bars_from(flat_then(rising_after()), s.open - timedelta(minutes=5 * 60))
    action, events, _ = feed_until_signal(st, bars)
    assert action is Action.NONE
    assert any(e["type"] == "SKIP" and e["reason"] == "DAILY_LOSS_LIMIT" for e in events)


def test_loss_just_above_limit_still_enters():
    s = session()
    st = Strategy("A", s, realized_pnl=-settings.DAILY_LOSS_LIMIT_USD + 2.5)
    bars = bars_from(flat_then(rising_after()), s.open - timedelta(minutes=5 * 60))
    action, _, _ = feed_until_signal(st, bars)
    assert action is Action.ENTER_LONG


def test_on_closed_accumulates_realized_pnl():
    st = Strategy("A", session())
    st.on_entry_filled(LONG)
    st.on_closed(-15.0)
    st.on_entry_filled(SHORT)
    st.on_closed(-10.0)
    assert st.realized_pnl == -25.0 and st.loss_limit_hit
