"""EMA 교차 + ATR 검증 전략. **순수 로직이고 API 를 부르지 않습니다.** A·B 가 같은 코드를 씁니다.

규칙 (Notion 「3주차 설계」 + GitHub 이슈 #1~#3)
1. 신호: 확정된 5분봉에서 EMA(9) 가 EMA(21) 를 상향 돌파 → 매수, 하향 돌파 → 매도.
   돌파 = 직전 봉에서 fast ≤ slow 였고 이번 봉에서 fast > slow (하향은 반대).
2. 진입 (보유 없을 때만)
   - 신호 봉이 신규 진입 구간 안: 봉 시작 ≥ 장 시작, 봉 종료 ≤ 마감 45분 전
   - 하루 신규 진입 5회 미만
   - 그날 실현 손익 합이 -25 USD 보다 큼 (도달하면 `DAILY_LOSS_LIMIT` 로 신규 진입 중단)
   - B 만: ATR_t ÷ (직전 20개 봉 ATR 평균) 이 0.90~1.30
   - 손절 거리가 1틱 이상 (아래 3)
3. 손절·익절 (A·B 같음, 진입 당시 ATR 기준, 틱에 맞출 때 **둘 다 진입가 쪽으로**)
   - 매수: 손절 = 진입 − 1.5·ATR 을 올림, 익절 = 진입 + 2·ATR 을 내림
   - 매도: 손절 = 진입 + 1.5·ATR 을 내림, 익절 = 진입 − 2·ATR 을 올림
   - 손절 거리가 1틱 미만이면 진입하지 않습니다(`stop_below_1tick`).
4. 보유 중 반대 교차: ATR 필터와 무관하게 **청산만** 합니다. 반대로 바로 들어가지 않습니다.
   다시 들어가려면 그 뒤에 **새 교차**가 나와야 합니다.
5. 마감 30분 전부터 강제 청산, 장 마감에 종료.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any

import trader  # noqa: F401  (week02 경로 등록)
from bot.clock import KST  # week02
from bot.strategy import LONG, SHORT, Bar, confirmed  # week02 — 봉 모양과 확정봉 판정은 그대로 씁니다
from trader import settings
from trader.indicators import atr, atr_base, ema

__all__ = ["LONG", "SHORT", "Bar", "confirmed", "Session", "Levels", "levels", "Row", "analyze",
           "Action", "Decision", "Strategy", "Trade", "STOP", "TAKE", "FORCE", "REVERSE"]

STOP, TAKE, FORCE, REVERSE = "STOP", "TAKE", "FORCE", "REVERSE_CROSS"
_EPS = 1e-9


# ── 운영 시간 ───────────────────────────────────────────────

@dataclass(frozen=True)
class Session:
    """그날 장. 시각은 모두 KST."""

    open: datetime                       # 봇 운영 시작
    close: datetime                      # 봇 운영 종료(RUN_END)
    trade_end: datetime | None = None    # 실제 거래 마감. 있으면 진입 마감·강제 청산을 여기서 셉니다

    @classmethod
    def of(cls, day: date, opened: time, closed: time, trade_end: time | None = None) -> Session:
        start = datetime.combine(day, opened, tzinfo=KST)
        end = datetime.combine(day, closed, tzinfo=KST)
        if end <= start:  # 자정을 넘는 장
            end += timedelta(days=1)
        last = None
        if trade_end is not None:
            last = datetime.combine(day, trade_end, tzinfo=KST)
            if last <= start:
                last += timedelta(days=1)
            if last > end:  # 운영 시간 밖이면 쓰지 않습니다(예: 주간 --session 에 야간 마감 06:15)
                last = None
        return cls(start, end, last)

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Session:
        last = d.get("trade_end")
        return cls(datetime.fromisoformat(d["open"]), datetime.fromisoformat(d["close"]),
                   datetime.fromisoformat(last) if last else None)

    @property
    def last_trade(self) -> datetime:
        return self.trade_end or self.close

    @property
    def entry_cutoff(self) -> datetime:
        return self.last_trade - timedelta(minutes=settings.ENTRY_CUTOFF_MIN_BEFORE_CLOSE)

    @property
    def force_close(self) -> datetime:
        return self.last_trade - timedelta(minutes=settings.FORCE_CLOSE_MIN_BEFORE_CLOSE)

    def to_dict(self) -> dict[str, str | None]:
        return {"open": self.open.isoformat(), "close": self.close.isoformat(),
                "trade_end": self.trade_end.isoformat() if self.trade_end else None,
                "entry_cutoff": self.entry_cutoff.isoformat(), "force_close": self.force_close.isoformat()}


# ── 손절·익절 ───────────────────────────────────────────────

@dataclass(frozen=True)
class Levels:
    side: str
    entry: float
    stop: float
    take: float
    stop_ticks: int
    take_ticks: int
    atr: float


def distance_ticks(atr_value: float, multiple: float, tick: float = settings.TICK) -> int:
    """ATR 배수 거리를 틱으로. 진입가 쪽으로 맞추므로 내림입니다."""
    return math.floor(multiple * atr_value / tick + _EPS)


def levels(side: str, entry: float, atr_value: float, tick: float = settings.TICK) -> Levels | None:
    """진입가와 진입 당시 ATR 로 손절·익절가. 손절이나 익절 거리가 1틱 미만이면 None.

    진입가는 틱 위에 있으므로, 거리를 틱 단위로 내린 뒤 더하고 빼면
    매수 손절 올림·익절 내림 / 매도 손절 내림·익절 올림과 같습니다.
    """
    stop_ticks = distance_ticks(atr_value, settings.STOP_ATR, tick)
    take_ticks = distance_ticks(atr_value, settings.TAKE_ATR, tick)
    if stop_ticks < 1 or take_ticks < 1:
        return None
    entry_t = round(entry / tick)
    sign = 1 if side == LONG else -1
    digits = max(0, -int(math.floor(math.log10(tick)))) + 2
    return Levels(
        side=side,
        entry=round(entry_t * tick, digits),
        stop=round((entry_t - sign * stop_ticks) * tick, digits),
        take=round((entry_t + sign * take_ticks) * tick, digits),
        stop_ticks=stop_ticks,
        take_ticks=take_ticks,
        atr=atr_value,
    )


@dataclass
class Trade:
    side: str
    qty: int
    entry_price: float
    stop: float
    take: float
    stop_ticks: int
    take_ticks: int
    atr: float
    entry_order: str | None
    entry_ts: str
    signal_bar: str | None = None      # 신호 봉 시작 시각(ISO). B 가 A 의 거래와 맞춰 볼 때 씁니다.
    restored: bool = False

    @classmethod
    def from_levels(cls, lv: Levels, qty: int, order_no: str | None, ts: str, *,
                    signal_bar: str | None = None, restored: bool = False) -> Trade:
        return cls(lv.side, qty, lv.entry, lv.stop, lv.take, lv.stop_ticks, lv.take_ticks, lv.atr,
                   order_no, ts, signal_bar, restored)

    def trigger(self, price: float) -> str | None:
        if self.side == LONG:
            return STOP if price <= self.stop else TAKE if price >= self.take else None
        return STOP if price >= self.stop else TAKE if price <= self.take else None

    def pnl_usd(self, exit_price: float) -> float:
        sign = 1 if self.side == LONG else -1
        return round((exit_price - self.entry_price) * sign * settings.POINT_VALUE * self.qty, 2)

    def ticks(self, exit_price: float) -> float:
        sign = 1 if self.side == LONG else -1
        return round((exit_price - self.entry_price) * sign / settings.TICK, 2)


# ── 지표 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Row:
    """확정봉 하나와 그 봉 마감 시점의 지표."""

    bar: Bar
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    atr_base: float | None
    cross: str | None

    @property
    def atr_ratio(self) -> float | None:
        return self.atr / self.atr_base if self.atr is not None and self.atr_base else None

    def to_dict(self) -> dict[str, Any]:
        def r(v: float | None, n: int = 7) -> float | None:
            return None if v is None else round(v, n)
        return {"bar": {"start": self.bar.start.isoformat(), "end": self.bar.end.isoformat(),
                        "open": self.bar.open, "high": self.bar.high, "low": self.bar.low,
                        "close": self.bar.close, "volume": self.bar.volume},
                "ema_fast": r(self.ema_fast), "ema_slow": r(self.ema_slow), "atr": r(self.atr),
                "atr_base": r(self.atr_base), "atr_ratio": r(self.atr_ratio, 4), "cross": self.cross}


def analyze(bars: list[Bar]) -> list[Row]:
    closes = [b.close for b in bars]
    fast = ema(closes, settings.EMA_FAST)
    slow = ema(closes, settings.EMA_SLOW)
    atrs = atr(bars, settings.ATR_PERIOD)
    base = atr_base(atrs, settings.ATR_BASE_BARS)
    rows: list[Row] = []
    for i, bar in enumerate(bars):
        cross = None
        if i > 0 and None not in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
            if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:  # type: ignore[operator]
                cross = LONG
            elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:  # type: ignore[operator]
                cross = SHORT
        rows.append(Row(bar, fast[i], slow[i], atrs[i], base[i], cross))
    return rows


# ── 상태 ────────────────────────────────────────────────────

class Action(str, Enum):
    NONE = "NONE"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"                      # 반대 교차 청산


@dataclass
class Decision:
    action: Action = Action.NONE
    events: list[dict[str, Any]] = field(default_factory=list)
    row: Row | None = None
    levels: Levels | None = None       # 진입 판단에 쓴 거리(신호 봉 종가 기준). 실제 가격은 체결가로 다시 셉니다.


class Strategy:
    """variant "A" 는 기본형, "B" 는 ATR 필터를 더한 개선형."""

    def __init__(self, variant: str, session: Session, *, entries_done: int = 0,
                 realized_pnl: float = 0.0) -> None:
        if variant not in ("A", "B"):
            raise ValueError(f"전략은 A 또는 B 입니다: {variant!r}")
        self.variant = variant
        self.session = session
        self.entries = entries_done
        self.realized_pnl = realized_pnl   # 그날 청산된 거래 손익 합(USD)
        self.position: str | None = None
        self._last_start: datetime | None = None

    @property
    def state(self) -> str:
        return f"IN_{self.position}" if self.position else "FLAT"

    def on_bars(self, bars: list[Bar], now: datetime) -> Decision:
        """새로 확정된 봉을 처리합니다. 행동은 **가장 최근 봉**에서만 냅니다(밀린 봉은 기록만)."""
        decision = Decision()
        done = confirmed(bars, now)
        rows = analyze(done)
        new = [r for r in rows if self._last_start is None or r.bar.start > self._last_start]
        for index, row in enumerate(new):
            latest = index == len(new) - 1
            decision.events.append({"type": "BAR", **row.to_dict()})
            if row.cross and row.bar.start >= self.session.open:
                decision.events.append({"type": "CROSS", "side": row.cross, "bar_start": row.bar.start.isoformat(),
                                        "atr": row.atr, "atr_ratio": row.atr_ratio, "latest": latest})
            if latest and row.cross:
                self._decide(row, decision)
            self._last_start = row.bar.start
        return decision

    def _decide(self, row: Row, decision: Decision) -> None:
        side = row.cross
        assert side is not None
        if self.position is not None:
            if side != self.position:
                decision.action = Action.EXIT
                decision.row = row
                decision.events.append({"type": "EXIT_SIGNAL", "reason": REVERSE, "position": self.position,
                                        "cross": side, "note": "반대 교차 → 청산만, 반대 진입 없음"})
            return

        def skip(reason: str, **extra: Any) -> None:
            decision.events.append({"type": "SKIP", "side": side, "reason": reason,
                                    "bar_start": row.bar.start.isoformat(), **extra})

        bar = row.bar
        if bar.start < self.session.open:
            return  # 장 시작 전 봉(지표 미리 채우기용)은 판단하지 않습니다
        if bar.end > self.session.entry_cutoff:
            return skip("OUTSIDE_ENTRY_WINDOW", entry_cutoff=self.session.entry_cutoff.isoformat())
        if self.entries >= settings.MAX_ENTRIES_PER_DAY:
            return skip("MAX_ENTRIES", entries=self.entries)
        if self.loss_limit_hit:
            return skip("DAILY_LOSS_LIMIT", realized_pnl=round(self.realized_pnl, 2),
                        limit=-settings.DAILY_LOSS_LIMIT_USD)
        if row.atr is None:
            return skip("ATR_WARMUP")
        if self.variant == "B":
            ratio = row.atr_ratio
            low, high = settings.ATR_BAND
            if ratio is None:
                return skip("ATR_WARMUP", note="ATR 기준값(직전 20개 평균)을 아직 낼 수 없음")
            if not low <= ratio <= high:
                decision.events.append({"type": "ATR_FILTER_BLOCK", "side": side, "atr": row.atr,
                                        "atr_base": row.atr_base, "atr_ratio": round(ratio, 4),
                                        "band": list(settings.ATR_BAND), "bar_start": bar.start.isoformat()})
                return
        lv = levels(side, bar.close, row.atr)
        if lv is None:
            return skip("stop_below_1tick", atr=row.atr,
                        stop_ticks=distance_ticks(row.atr, settings.STOP_ATR),
                        take_ticks=distance_ticks(row.atr, settings.TAKE_ATR))
        decision.action = Action.ENTER_LONG if side == LONG else Action.ENTER_SHORT
        decision.row = row
        decision.levels = lv
        decision.events.append({"type": "ENTRY_SIGNAL", "side": side, "variant": self.variant,
                                "levels_at_signal": asdict(lv), "atr_ratio": row.atr_ratio,
                                "bar_start": bar.start.isoformat()})

    # ── 결과 통지 ───────────────────────────────────────────

    def on_entry_filled(self, side: str) -> None:
        self.position = side
        self.entries += 1

    def restore_position(self, side: str) -> None:
        self.position = side

    @property
    def loss_limit_hit(self) -> bool:
        return self.realized_pnl <= -settings.DAILY_LOSS_LIMIT_USD

    def on_closed(self, pnl_usd: float | None = None) -> None:
        self.position = None
        if pnl_usd is not None:
            self.realized_pnl += pnl_usd
