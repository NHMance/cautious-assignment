"""5분봉 조회·확정봉 판정, 현재가.

필드 매핑은 참고 저장소의 녹화본(`tests/fixtures/nh/domestic_chart-minute`)에서 확인했습니다.
지금 이 저장소의 `tests/fixtures/nh` 는 봇 테스트용 목업입니다.
- 줄 배열 `Occurs1`, 최신 봉이 먼저 옵니다(내림차순).
- `occurs1_ref_dt`(YYYYMMDD) + `occurs1_exch_tm`(HHMMSS) 는 **봉 시작 시각**입니다.
  09:01:01 에 받은 5분봉 응답에 `090000` 봉이 들어 있으므로(진행 중 봉) 시작 시각입니다.
- 가격 `occurs1_open_pric`·`high_pric`·`low_pric`·`close_pric`, 거래량 `occurs1_cum_trd_qty`.
- 현재가는 `domestic_price` 응답의 `last_pric`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from backend.domestic_futureoption import domestic_futureoption_functions as api
from bot import settings
from bot.clock import KST
from bot.strategy import Bar, confirmed

MAX_PAGES = 5


def number(raw: Any) -> float | None:
    """`"00000000001335.900000000"`·`"1338.50"` → float. 빈 값은 None."""
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_bars(raw: dict[str, Any], *, day: date, minutes: int = settings.BAR_MINUTES) -> list[Bar]:
    rows = raw.get("Occurs1")
    rows = rows if isinstance(rows, list) else []
    made: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref_dt = str(row.get("occurs1_ref_dt") or "").strip()
        tm = str(row.get("occurs1_exch_tm") or "").strip().zfill(6)
        if ref_dt != day.strftime("%Y%m%d"):
            continue
        try:
            start = datetime.strptime(ref_dt + tm[:6], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        prices = [number(row.get(f"occurs1_{k}_pric")) for k in ("open", "high", "low", "close")]
        if any(p is None for p in prices):
            continue
        o, h, l, c = prices
        made.append(Bar(start, start + timedelta(minutes=minutes), o, h, l, c, int(number(row.get("occurs1_cum_trd_qty")) or 0)))
    made.sort(key=lambda b: b.start)
    return made


class Market:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def fetch_bars(self, day: date, minutes: int = settings.BAR_MINUTES) -> list[Bar]:
        """그날 `minutes` 분봉 전부(진행 중 봉 포함) 시간순으로."""
        ymd = day.strftime("%Y%m%d")
        bars: dict[datetime, Bar] = {}
        next_key = api.FIRST_PAGE
        for _ in range(MAX_PAGES):
            raw = api.chart_minute(
                self.symbol,
                start_date=ymd,
                end_date=ymd,
                start_time="084500",
                end_time="153000",
                minutes=str(minutes),
                next_key=next_key,
            )
            rows = raw.get("Occurs1") if isinstance(raw.get("Occurs1"), list) else []
            for b in parse_bars(raw, day=day, minutes=minutes):
                bars.setdefault(b.start, b)
            key = str(raw.get("nxt_key") or "").strip()
            # 한 쪽이 꽉 찼을 때만 이어 받습니다. 꽉 차지 않아도 키가 오므로 키만 보면 끝없이 돕니다.
            if not key or key == next_key or len(rows) < int(api.CHART_ROWS):
                break
            next_key = key
        return sorted(bars.values(), key=lambda b: b.start)

    def get_bars_5m(self, today: date, now: datetime) -> list[Bar]:
        """오늘 5분봉 중 **확정봉만** 시간순으로."""
        return confirmed(self.fetch_bars(today), now)

    def get_price(self) -> float:
        raw = api.price(self.symbol)
        value = number(raw.get("last_pric"))
        if value is None or value <= 0:
            raise ValueError(f"현재가를 읽지 못했습니다: last_pric={raw.get('last_pric')!r}")
        return value
