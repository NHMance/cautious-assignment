"""봇 테스트용 합성 봉. 서버·인증 없이 흐름만 점검합니다(`--bot-test`).

MKU 모양(0.7476 근처, 틱 0.0001)으로 1분봉을 만들고 5분봉은 그것을 묶어 만듭니다 — 두 봉이 서로 맞습니다.
변동성이 낮음 → 보통 → 높음으로 바뀌는 구간과 추세 반전을 섞어, B 의 ATR 필터가 일부 신호를 거르도록 했습니다.
시드가 고정이라 매번 같은 봉이 나옵니다.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from trader import settings
from trader.strategy import Bar, Session


def _minute_bars(start: datetime, minutes: int, rng: random.Random, price: float) -> tuple[list[Bar], float]:
    tick = settings.TICK
    out: list[Bar] = []
    for i in range(minutes):
        phase = (i // 90) % 3
        vol = (0.3, 1.0, 4.0)[phase]                         # 1분당 틱 단위 변동: 낮음→보통→높음
        drift = 0.35 if (i // 75) % 2 == 0 else -0.35
        o = price
        c = round(o + round(drift + rng.gauss(0, vol)) * tick, 4)
        h = round(max(o, c) + abs(round(rng.gauss(0, vol / 2))) * tick, 4)
        low = round(min(o, c) - abs(round(rng.gauss(0, vol / 2))) * tick, 4)
        t = start + timedelta(minutes=i)
        out.append(Bar(t, t + timedelta(minutes=1), o, h, low, c, rng.randint(1, 30)))
        price = c
    return out, price


def aggregate(bars_1m: list[Bar], minutes: int = settings.BAR_MINUTES) -> list[Bar]:
    groups: dict[datetime, list[Bar]] = {}
    for b in bars_1m:
        key = b.start.replace(minute=b.start.minute - b.start.minute % minutes, second=0, microsecond=0)
        groups.setdefault(key, []).append(b)
    out = []
    for key in sorted(groups):
        g = groups[key]
        out.append(Bar(key, key + timedelta(minutes=minutes), g[0].open, max(b.high for b in g),
                       min(b.low for b in g), g[-1].close, sum(b.volume for b in g)))
    return out


def make_day(session: Session, *, seed: int = 2) -> tuple[list[Bar], list[Bar]]:
    """(5분봉, 1분봉). 5분봉에는 전날 장(지표 미리 채우기용)이 앞에 붙습니다. 1분봉은 이번 장만."""
    rng = random.Random(seed)
    length = int((session.close - session.open).total_seconds() // 60)
    prev_open = session.open - timedelta(days=1)
    prev_1m, price = _minute_bars(prev_open, length, rng, 0.7476)
    today_1m, _ = _minute_bars(session.open, length, rng, price)
    return aggregate(prev_1m) + aggregate(today_1m), today_1m
