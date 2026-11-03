"""EMA·ATR. 순수 계산이고 API 를 부르지 않습니다.

- EMA(n): 첫 값은 앞 n개 종가의 단순평균, 그 뒤로 α = 2/(n+1).
- TR = max(고가−저가, |고가−전봉종가|, |저가−전봉종가|). 첫 봉은 고가−저가.
- ATR(n): Wilder 평활. 첫 값은 앞 n개 TR 의 평균, 그 뒤로 (이전ATR×(n−1) + TR)/n.
- ATR 기준값: 직전 m개 봉의 ATR 평균 (현재 봉 제외). 현재 봉 ATR 은 봉 마감 후 확정된 값.
- ATR 비율: 현재 봉 ATR ÷ 기준값.

값을 낼 수 없는 앞부분은 None 입니다.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class OHLC(Protocol):
    high: float
    low: float
    close: float


def ema(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2 / (n + 1)
    current = sum(values[:n]) / n
    out[n - 1] = current
    for i in range(n, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        out[i] = current
    return out


def true_range(bars: Sequence[OHLC]) -> list[float]:
    out: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            out.append(bar.high - bar.low)
            continue
        prev = bars[i - 1].close
        out.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    return out


def atr(bars: Sequence[OHLC], n: int) -> list[float | None]:
    tr = true_range(bars)
    out: list[float | None] = [None] * len(tr)
    if len(tr) < n:
        return out
    current = sum(tr[:n]) / n
    out[n - 1] = current
    for i in range(n, len(tr)):
        current = (current * (n - 1) + tr[i]) / n
        out[i] = current
    return out


def atr_base(atrs: Sequence[float | None], m: int) -> list[float | None]:
    """직전 m개 봉의 ATR 평균. 현재 봉의 ATR 은 넣지 않습니다(이슈 #2)."""
    out: list[float | None] = [None] * len(atrs)
    for i in range(m, len(atrs)):
        window = atrs[i - m : i]
        if all(v is not None for v in window):
            out[i] = sum(window) / m  # type: ignore[arg-type]
    return out


def atr_ratio(atrs: Sequence[float | None], m: int) -> list[float | None]:
    """현재 봉 ATR ÷ 직전 m개 평균. 낼 수 없으면 None."""
    base = atr_base(atrs, m)
    return [a / b if a is not None and b else None for a, b in zip(atrs, base)]
