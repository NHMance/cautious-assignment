"""전략 설정·시세 자료형·진입 및 청산 규칙."""
from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import datetime, time, timedelta
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo
import tomllib
from collections import deque
from statistics import mean, pstdev

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
TICK = 0.1
MULTIPLIER = 10000  # USD 10,000: 가격 1원 변동 = 10,000원/계약


@dataclass(frozen=True)
class Settings:
    window: int = 24
    entry_z: float = 1.5
    exit_z: float = 0.25
    stop_z: float = 2.5
    max_hold_minutes: int = 60
    max_entries: int = 3
    trade_loss_krw: float = 10000
    daily_loss_krw: float = 30000
    entry_cutoff: str = "15:00"
    force_close: str = "15:10"
    deadline: str = "15:20"
    fee_per_leg_side: float = 0
    slippage_ticks: int = 1
    max_book_ticks: int = 2
    max_quote_age_seconds: float = 10
    max_quote_skew_seconds: float = 3

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if not isinstance(v, str) and (not isfinite(v) or v < 0):
                raise ValueError(f"설정값 오류: {f.name}")
        if self.window < 2 or not 0 <= self.exit_z < self.entry_z < self.stop_z:
            raise ValueError("window>=2, 0<=exit_z<entry_z<stop_z 여야 합니다")
        for name in ("window", "max_entries", "max_hold_minutes", "slippage_ticks", "max_book_ticks"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} 은 정수여야 합니다")
        if min(self.max_entries, self.max_hold_minutes, self.trade_loss_krw, self.daily_loss_krw) <= 0:
            raise ValueError("횟수·보유시간·손실한도는 양수여야 합니다")
        if not time(9) < time.fromisoformat(self.entry_cutoff) < time.fromisoformat(self.force_close) < time.fromisoformat(self.deadline) <= time(15, 45):
            raise ValueError("운영시간 순서를 확인하세요")

    @classmethod
    def load(cls, path: Path = ROOT / "settings.toml"):
        return cls(**tomllib.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: float
    close: float
    volume: int

    @property
    def end(self):
        return self.start + timedelta(minutes=5)


@dataclass(frozen=True)
class PairBar:
    near: Bar
    far: Bar

    @property
    def ts(self):
        return self.near.end

    @property
    def spread(self):
        return self.far.close - self.near.close


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime
    bid: float
    ask: float
    bid_qty: int = 1
    ask_qty: int = 1

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def price(self, side: int):
        return self.ask if side == 1 else self.bid


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: int  # +1 매수, -1 매도
    price: float
    ts: datetime
    order_no: str


def at(day, value: str):
    return datetime.combine(day, time.fromisoformat(value), KST)


def quote_problem(near: Quote, far: Quote, now: datetime, cfg: Settings):
    for q in (near, far):
        if not all(isfinite(x) for x in (q.bid, q.ask)) or q.bid <= 0 or q.ask < q.bid:
            return "INVALID_BOOK"
        if not 0 <= (now - q.ts).total_seconds() <= cfg.max_quote_age_seconds:
            return "STALE_QUOTE"
    if abs((near.ts - far.ts).total_seconds()) > cfg.max_quote_skew_seconds:
        return "QUOTE_TIME_SKEW"
    for q in (near, far):
        if min(q.bid_qty, q.ask_qty) < 1:
            return "INSUFFICIENT_DEPTH"
        if q.ask - q.bid > cfg.max_book_ticks * TICK + 1e-8:
            return "WIDE_BOOK"
    return None


@dataclass(frozen=True)
class Signal:
    ts: object
    direction: int  # +1 스프레드 매수(근월 매도/차월 매수), -1 스프레드 매도
    mean: float
    std: float
    z: float
    spread: float


@dataclass
class Position:
    signal: Signal
    near: Fill
    far: Fill

    @property
    def entered_at(self):
        return min(self.near.ts, self.far.ts)

    def pnl(self, near_price, far_price):
        return round((
            self.near.side * (near_price - self.near.price)
            + self.far.side * (far_price - self.far.price)
        ) * MULTIPLIER, 6)

    def liquidation_pnl(self, nq: Quote, fq: Quote, fee: float):
        return self.pnl(nq.price(-self.near.side), fq.price(-self.far.side)) - fee * 4


class Strategy:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.window = deque(maxlen=cfg.window)
        self.last = None

    def observe(self, pair: PairBar):
        """신호·불성립 사유·통계값을 반환합니다. 결측 후에는 연속 24개를 다시 모읍니다."""
        if self.last is not None and pair.ts <= self.last:
            return None, "DUPLICATE_OR_OLD", {}
        if self.last is not None and (pair.ts - self.last != timedelta(minutes=5) or pair.ts.date() != self.last.date()):
            self.window.clear()
        self.last = pair.ts
        if min(pair.near.volume, pair.far.volume) < 1:
            self.window.clear()
            return None, "NO_VOLUME", {}
        stats = {}
        signal = None
        reason = "WARMUP"
        if len(self.window) == self.cfg.window:
            mu, sd = mean(self.window), pstdev(self.window)
            stats = {"mean": mu, "std": sd, "spread": pair.spread}
            if sd <= 1e-8:
                reason = "ZERO_STD"
            else:
                z = (pair.spread - mu) / sd
                stats["z"] = z
                reason = "NO_SIGNAL"
                if abs(z) + 1e-8 >= self.cfg.stop_z:
                    reason = "BEYOND_STOP_BAND"
                elif abs(z) + 1e-8 >= self.cfg.entry_z:
                    direction = -1 if z > 0 else 1
                    signal = Signal(pair.ts, direction, mu, sd, z, pair.spread)
                    reason = "SIGNAL"
        self.window.append(pair.spread)  # 판정 후 현재값을 추가하여 미래 참조를 방지합니다.
        return signal, reason, stats


def exit_reason(pos: Position, nq: Quote, fq: Quote, now, cfg: Settings, realized: float):
    pnl = pos.liquidation_pnl(nq, fq, cfg.fee_per_leg_side)
    if now >= at(now.date(), cfg.force_close):
        return "FORCE_CLOSE"
    if realized + pnl <= -cfg.daily_loss_krw:
        return "DAILY_LOSS"
    if pnl <= -cfg.trade_loss_krw:
        return "TRADE_LOSS"
    if now - pos.entered_at >= timedelta(minutes=cfg.max_hold_minutes):
        return "TIME_STOP"
    z = ((fq.mid - nq.mid) - pos.signal.mean) / pos.signal.std
    if -pos.signal.direction * z >= cfg.stop_z:
        return "Z_STOP"
    if pos.signal.direction * z >= -cfg.exit_z:
        return "MEAN_REVERSION"
    return None


def entry_problem(signal, nq, fq, cfg):
    """신호와 주문 사이에 이미 수렴했거나 손절 경계를 넘었으면 진입하지 않습니다."""
    z = ((fq.mid - nq.mid) - signal.mean) / signal.std
    adverse_z = -signal.direction * z
    if adverse_z >= cfg.stop_z:
        return "BEYOND_STOP_BEFORE_ENTRY"
    if adverse_z + 1e-8 < cfg.entry_z:
        return "REVERTED_BEFORE_ENTRY"
    return None
