"""상태 머신 단위 테스트. 가상 봉 데이터만 씁니다."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from bot.clock import KST, at
from bot.strategy import Action, Bar, State, Strategy

DAY = date(2026, 10, 28)
H, L = 110.0, 90.0


def bar(hhmm: str, close: float, *, high: float | None = None, low: float | None = None) -> Bar:
    start = at(DAY, hhmm)
    return Bar(
        start=start,
        end=start + timedelta(minutes=5),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
    )


def range_bars() -> list[Bar]:
    """09:00~09:15 네 봉. H=110, L=90."""
    return [
        bar("09:00", 100, high=105, low=95),
        bar("09:05", 102, high=H, low=98),
        bar("09:10", 97, high=104, low=L),
        bar("09:15", 100, high=103, low=96),
    ]


def after(hhmm: str, seconds: int = 5) -> datetime:
    return at(DAY, hhmm) + timedelta(seconds=seconds)


def feed(strategy: Strategy, bars: list[Bar], hhmm: str):
    """그 시각(+5초)에 받은 봉 목록으로 판단."""
    return strategy.on_bars(bars, after(hhmm))


def types(decision) -> list[str]:
    return [event["type"] for event in decision.events if event["type"] != "BAR"]


def test_range_set_from_four_bars():
    s = Strategy(DAY)
    d = feed(s, range_bars(), "09:20")
    assert s.state is State.WATCH
    assert (s.high, s.low) == (H, L)
    assert "RANGE_SET" in types(d)


def test_1_breakout_up_then_confirm_enters_long():
    s = Strategy(DAY)
    bars = range_bars()
    feed(s, bars, "09:20")
    bars.append(bar("09:20", 111))
    d = feed(s, bars, "09:25")
    assert s.state is State.CONFIRM_LONG
    assert types(d) == ["BREAKOUT"]
    bars.append(bar("09:25", 112))
    d = feed(s, bars, "09:30")
    assert d.action is Action.ENTER_LONG
    assert s.state is State.ENTERING
    assert types(d) == ["CONFIRM_OK"]


def test_2_breakout_up_then_fail_returns_to_watch():
    s = Strategy(DAY)
    bars = range_bars()
    feed(s, bars, "09:20")
    bars += [bar("09:20", 111)]
    feed(s, bars, "09:25")
    bars += [bar("09:25", 110)]  # 종가 ≤ H
    d = feed(s, bars, "09:30")
    assert d.action is Action.NONE
    assert s.state is State.WATCH
    assert types(d) == ["CONFIRM_FAIL"]


def test_3_breakout_down_then_confirm_enters_short():
    s = Strategy(DAY)
    bars = range_bars()
    feed(s, bars, "09:20")
    bars += [bar("09:20", 89)]
    feed(s, bars, "09:25")
    assert s.state is State.CONFIRM_SHORT
    bars += [bar("09:25", 88)]
    d = feed(s, bars, "09:30")
    assert d.action is Action.ENTER_SHORT


def test_4_confirm_bar_ending_after_cutoff_does_not_enter():
    s = Strategy(DAY)
    bars = range_bars()
    feed(s, bars, "09:20")
    bars += [bar("14:55", 111)]  # 돌파 봉 14:55~15:00
    feed(s, bars, "15:00")
    bars += [bar("15:00", 112)]  # 재확인 봉 15:00~15:05 → 종료가 15:00 이후
    d = feed(s, bars, "15:05")
    assert d.action is Action.NONE
    assert s.state is State.DONE


def test_4b_confirm_bar_ending_exactly_at_cutoff_enters():
    s = Strategy(DAY)
    bars = range_bars()
    feed(s, bars, "09:20")
    bars += [bar("14:50", 111)]
    feed(s, bars, "14:55")
    bars += [bar("14:55", 112)]  # 14:55~15:00 봉까지 가능
    d = feed(s, bars, "15:00")
    assert d.action is Action.ENTER_LONG


def test_5_no_entry_after_two_entries():
    s = Strategy(DAY)
    bars = range_bars()
    feed(s, bars, "09:20")
    # 첫 진입
    bars += [bar("09:20", 111), bar("09:25", 112)]
    feed(s, bars[:-1], "09:25")
    assert feed(s, bars, "09:30").action is Action.ENTER_LONG
    s.on_entry_filled("LONG")
    bars += [bar("09:30", 115)]
    feed(s, bars, "09:35")
    s.on_exit_started()
    s.on_closed(after("09:36"))
    assert s.state is State.WATCH
    # 두 번째 진입: 기준선 안으로 돌아온 뒤 새 돌파
    bars += [bar("09:35", 105), bar("09:40", 111), bar("09:45", 113)]
    feed(s, bars[:-2], "09:40")
    feed(s, bars[:-1], "09:45")
    assert feed(s, bars, "09:50").action is Action.ENTER_LONG
    s.on_entry_filled("LONG")
    s.on_exit_started()
    s.on_closed(after("09:51"))
    assert s.state is State.DONE
    # 추가 신호 → 진입 없음
    bars += [bar("09:50", 100), bar("09:55", 111), bar("10:00", 112)]
    for hhmm, upto in (("09:55", -2), ("10:00", -1)):
        feed(s, bars[:upto], hhmm)
    assert feed(s, bars, "10:05").action is Action.NONE
    assert s.state is State.DONE


def test_6_missing_range_bar_means_no_trade():
    s = Strategy(DAY)
    bars = [b for b in range_bars() if b.start.strftime("%H:%M") != "09:10"]
    feed(s, bars, "09:20")
    assert s.state is State.WAIT_RANGE  # 한 봉은 더 기다립니다
    bars += [bar("09:20", 120)]
    d = feed(s, bars, "09:25")
    assert s.state is State.DONE
    assert s.done_reason == "RANGE_INCOMPLETE"
    done = [e for e in d.events if e["type"] == "DONE"][0]
    assert done["missing"] == ["09:10"]
    bars += [bar("09:25", 121)]
    assert feed(s, bars, "09:30").action is Action.NONE


def test_7_in_progress_bar_is_ignored():
    s = Strategy(DAY)
    bars = range_bars()
    # 09:17 에 받은 응답: 09:15 봉은 아직 진행 중(09:20 종료) → 기준가를 정하면 안 됨
    d = s.on_bars(bars, at(DAY, "09:17"))
    assert s.state is State.WAIT_RANGE
    assert len([e for e in d.events if e["type"] == "BAR"]) == 3
    feed(s, bars, "09:20")
    assert s.state is State.WATCH
    # 진행 중 봉의 종가가 H 위여도 돌파로 보지 않음
    bars += [bar("09:20", 111)]
    s.on_bars(bars, at(DAY, "09:22"))
    assert s.state is State.WATCH


def test_reentry_needs_new_breakout():
    """청산 뒤 가격이 계속 H 위에 있으면 재진입하지 않습니다."""
    s = Strategy(DAY)
    bars = range_bars() + [bar("09:20", 111), bar("09:25", 112)]
    feed(s, bars[:-1], "09:25")
    assert feed(s, bars, "09:30").action is Action.ENTER_LONG
    s.on_entry_filled("LONG")
    s.on_exit_started()
    s.on_closed(after("09:31"))
    bars += [bar("09:30", 113), bar("09:35", 114)]
    feed(s, bars[:-1], "09:35")
    assert feed(s, bars, "09:40").action is Action.NONE
    assert s.state is State.WATCH


def test_catchup_does_not_enter_on_stale_signal():
    """재시작해 지난 봉을 한꺼번에 받으면 지나간 신호로 주문하지 않습니다."""
    s = Strategy(DAY)
    bars = range_bars() + [bar("09:20", 111), bar("09:25", 112), bar("09:30", 112)]
    d = feed(s, bars, "09:35")
    assert d.action is Action.NONE
    ok = [e for e in d.events if e["type"] == "CONFIRM_OK"][0]
    assert ok["reason"] == "STALE"


def test_cutoff_moves_watch_to_done():
    s = Strategy(DAY)
    feed(s, range_bars(), "09:20")
    s.on_bars(range_bars(), datetime(2026, 10, 28, 15, 0, 5, tzinfo=KST))
    assert s.state is State.DONE
    assert s.done_reason == "CUTOFF"
