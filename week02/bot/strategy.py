"""장 초반 가격 돌파 재확인 전략 — 상태 머신. **순수 로직이고 API 를 부르지 않습니다.**

입력은 확정봉 목록과 현재 시각, 출력은 행동(`NONE`/`ENTER_LONG`/`ENTER_SHORT`)과
기록할 이벤트 목록입니다. 주문·체결·청산은 `main.py` 가 하고, 그 결과를
`on_entry_filled`·`on_closed` 같은 메서드로 알려 줍니다.

규칙 (README 전략 절과 1:1)
1. 09:00~09:20 확정 5분봉 4개(09:00·09:05·09:10·09:15 시작)로 H=max(high), L=min(low).
   4개가 모이지 않으면 당일 거래 없음.
2. WATCH: 직전 봉 종가 ≤ H 이고 이번 확정봉 종가 > H → 상향 돌파(CONFIRM_LONG).
          직전 봉 종가 ≥ L 이고 이번 확정봉 종가 < L → 하향 돌파(CONFIRM_SHORT).
3. CONFIRM: 다음 확정봉 종가가 여전히 밖(> H / < L)이면 진입, 아니면 거짓 돌파로 WATCH.
   재확인 봉의 종료 시각이 15:00 이하여야 진입합니다(14:55~15:00 봉까지).
4. 진입 2회 소진 또는 15:00 이후에는 신규 진입 없음(DONE).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from bot import settings
from bot.clock import at


class State(str, Enum):
    WAIT_RANGE = "WAIT_RANGE"
    WATCH = "WATCH"
    CONFIRM_LONG = "CONFIRM_LONG"
    CONFIRM_SHORT = "CONFIRM_SHORT"
    ENTERING = "ENTERING"
    IN_POSITION = "IN_POSITION"
    EXITING = "EXITING"
    DONE = "DONE"


class Action(str, Enum):
    NONE = "NONE"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"


LONG = "LONG"
SHORT = "SHORT"

# 신규 진입 신호를 볼 수 있는 상태.
_SIGNAL_STATES = (State.WAIT_RANGE, State.WATCH, State.CONFIRM_LONG, State.CONFIRM_SHORT)


@dataclass(frozen=True)
class Bar:
    """5분봉 하나. `start`·`end` 는 KST. 봉은 `end` 시각이 지나야 확정입니다."""

    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def confirmed(bars: list[Bar], now: datetime) -> list[Bar]:
    """확정봉만, 시간순으로. 진행 중인 봉(end > now)은 뺍니다. 같은 시작 시각은 뒤엣것."""
    by_start = {bar.start: bar for bar in bars if bar.end <= now}
    return [by_start[key] for key in sorted(by_start)]


@dataclass(frozen=True)
class Params:
    bar_minutes: int = settings.BAR_MINUTES
    range_start: str = settings.RANGE_START
    range_end: str = settings.RANGE_END
    entry_cutoff: str = settings.ENTRY_CUTOFF
    max_entries: int = settings.MAX_ENTRIES_PER_DAY


@dataclass
class Decision:
    action: Action = Action.NONE
    events: list[dict[str, Any]] = field(default_factory=list)
    signal_bar: Bar | None = None


class Strategy:
    def __init__(self, day: date, params: Params | None = None, *, entries_done: int = 0) -> None:
        self.p = params or Params()
        self.day = day
        self.range_start = at(day, self.p.range_start)
        self.range_end = at(day, self.p.range_end)
        self.cutoff = at(day, self.p.entry_cutoff)

        self.state = State.WAIT_RANGE
        self.high: float | None = None
        self.low: float | None = None
        self.entries = entries_done
        self.done_reason: str | None = None
        self.breakout_bar: Bar | None = None
        self.position_side: str | None = None

        self._last_start: datetime | None = None
        self._prev_close: float | None = None

    # ── 봉 처리 ───────────────────────────────────────────────

    def on_bars(self, bars: list[Bar], now: datetime) -> Decision:
        """새로 확정된 봉을 차례로 처리합니다. 이미 본 봉은 다시 보지 않습니다."""
        decision = Decision()
        bars = [bar for bar in confirmed(bars, now) if bar.start.date() == self.day]

        # 재시작해 보유 중으로 이어 가는 경우에도 H/L 은 구해 둡니다(청산 뒤 WATCH 용).
        if self.high is None and self.state is not State.DONE:
            self._try_range(bars, now, decision.events)

        new = [bar for bar in bars if self._last_start is None or bar.start > self._last_start]
        for index, bar in enumerate(new):
            latest = index == len(new) - 1
            decision.events.append({"type": "BAR", "bar": bar.to_dict()})
            if bar.start >= self.range_end:
                action = self._step(bar, latest, decision.events)
                if action is not Action.NONE:
                    decision.action = action
                    decision.signal_bar = bar
            self._prev_close = bar.close
            self._last_start = bar.start

        if decision.action is Action.NONE:
            self._apply_cutoff(now, decision.events)
        return decision

    def _try_range(self, bars: list[Bar], now: datetime, events: list[dict[str, Any]]) -> None:
        step = timedelta(minutes=self.p.bar_minutes)
        expected = []
        moment = self.range_start
        while moment < self.range_end:
            expected.append(moment)
            moment += step
        found = {bar.start: bar for bar in bars if self.range_start <= bar.start < self.range_end}

        if all(start in found for start in expected):
            used = [found[start] for start in expected]
            self.high = max(bar.high for bar in used)
            self.low = min(bar.low for bar in used)
            if self.state is State.WAIT_RANGE:
                self.state = State.WATCH
            events.append(
                {"type": "RANGE_SET", "H": self.high, "L": self.low, "bars": [bar.to_dict() for bar in used]}
            )
            return

        # 마지막 기준 봉이 확정되고 한 봉을 더 기다려도 모자라면 당일 거래하지 않습니다.
        if self.state is State.WAIT_RANGE and now >= self.range_end + step:
            missing = [start.strftime("%H:%M") for start in expected if start not in found]
            self._done("RANGE_INCOMPLETE", events, missing=missing)

    def _step(self, bar: Bar, latest: bool, events: list[dict[str, Any]]) -> Action:
        if self.state is State.WATCH:
            self._watch(bar, events)
            return Action.NONE

        if self.state in (State.CONFIRM_LONG, State.CONFIRM_SHORT):
            long = self.state is State.CONFIRM_LONG
            side = LONG if long else SHORT
            holds = bar.close > self.high if long else bar.close < self.low
            breakout = self.breakout_bar.to_dict() if self.breakout_bar else None
            if not holds:
                events.append({"type": "CONFIRM_FAIL", "side": side, "bar": bar.to_dict(), "breakout_bar": breakout})
                self.state = State.WATCH
                self.breakout_bar = None
                # 재확인에 실패한 봉이 반대쪽 돌파일 수 있어 WATCH 규칙으로 한 번 더 봅니다.
                self._watch(bar, events)
                return Action.NONE

            base = {"type": "CONFIRM_OK", "side": side, "bar": bar.to_dict(), "breakout_bar": breakout}
            if bar.end > self.cutoff:
                events.append({**base, "entry": False, "reason": "AFTER_CUTOFF"})
                self._done("AFTER_CUTOFF", events)
                return Action.NONE
            if self.entries >= self.p.max_entries:
                events.append({**base, "entry": False, "reason": "MAX_ENTRIES"})
                self._done("MAX_ENTRIES", events)
                return Action.NONE
            if not latest:
                # 재시작 직후처럼 지난 봉을 몰아서 볼 때. 이미 지나간 신호로는 주문하지 않습니다.
                events.append({**base, "entry": False, "reason": "STALE"})
                self.state = State.WATCH
                self.breakout_bar = None
                return Action.NONE
            events.append({**base, "entry": True})
            self.state = State.ENTERING
            return Action.ENTER_LONG if long else Action.ENTER_SHORT

        return Action.NONE

    def _watch(self, bar: Bar, events: list[dict[str, Any]]) -> None:
        prev = self._prev_close
        if prev is None:
            return
        if prev <= self.high and bar.close > self.high:
            self.state = State.CONFIRM_LONG
        elif prev >= self.low and bar.close < self.low:
            self.state = State.CONFIRM_SHORT
        else:
            return
        self.breakout_bar = bar
        side = LONG if self.state is State.CONFIRM_LONG else SHORT
        events.append({"type": "BREAKOUT", "side": side, "bar": bar.to_dict(), "prev_close": prev})

    def _apply_cutoff(self, now: datetime, events: list[dict[str, Any]]) -> None:
        if self.state in _SIGNAL_STATES and now >= self.cutoff:
            self._done("CUTOFF", events)

    def _done(self, reason: str, events: list[dict[str, Any]], **extra: Any) -> None:
        if self.state is State.DONE:
            return
        self.state = State.DONE
        self.done_reason = reason
        events.append({"type": "DONE", "reason": reason, **extra})

    # ── 주문 결과 통지 (main.py 가 부름) ───────────────────────

    def on_entry_failed(self, now: datetime) -> list[dict[str, Any]]:
        """리스크 차단·주문 실패·미체결. 새 돌파를 다시 기다립니다."""
        events: list[dict[str, Any]] = []
        self.state = State.WATCH
        self.breakout_bar = None
        self._apply_cutoff(now, events)
        return events

    def on_entry_filled(self, side: str) -> None:
        self.state = State.IN_POSITION
        self.position_side = side
        self.entries += 1
        self.breakout_bar = None

    def on_exit_started(self) -> None:
        self.state = State.EXITING

    def on_exit_failed(self) -> None:
        self.state = State.IN_POSITION

    def on_closed(self, now: datetime) -> list[dict[str, Any]]:
        """청산 체결 + 잔고 0 확인 뒤. 재진입은 새로운 돌파가 있어야 합니다."""
        events: list[dict[str, Any]] = []
        self.position_side = None
        if self.entries >= self.p.max_entries:
            self.state = State.WATCH
            self._done("MAX_ENTRIES", events)
            return events
        if self.high is None:
            self.state = State.WAIT_RANGE
        else:
            self.state = State.WATCH
        self._apply_cutoff(now, events)
        return events

    def restore_position(self, side: str) -> None:
        """재시작했는데 잔고가 있으면 보유 상태로 이어 갑니다."""
        self.state = State.IN_POSITION
        self.position_side = side
        self.entries = max(self.entries, 1)

    @property
    def holding(self) -> bool:
        return self.state in (State.IN_POSITION, State.EXITING)
