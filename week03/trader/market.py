"""해외 시세: 5분봉(지표를 전날 봉으로 미리 채움), 현재가(실시간 체결 우선, 없으면 REST), 장 시간.

필드 이름 (week02 backend 의 해외 파서·명세에서 확인)
- 현재가 `overseas_price` 줄: `occurs1_last_pric`, 실시간 종목코드 `occurs1_real_sym`.
- 장운영정보 `overseas_market-operations` 줄: `occurs1_exch_cd`·`occurs1_prd_cd`·`occurs1_trd_tm_inf`(1차, 한국시간).
- 차트(분) `overseas_chart-minute`: 줄 배열 `Occurs1`, **최신이 먼저**, 시각은 한국시간(backend 해외 시세 주석).

**아직 실서버로 확인하지 못한 것:** 해외 분봉 줄의 날짜·시각 칸 이름. 국내는 `occurs1_ref_dt`·`occurs1_exch_tm` 입니다.
그래서 후보 이름을 차례로 보고, 처음 받은 응답 원문을 `results/samples/` 에 남깁니다(인증 칸은 지움).
키를 넣고 한 번 돌린 뒤 그 파일로 확정합니다.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import trader  # noqa: F401  (week02 경로 등록)
from backend.overseas_futureoption import overseas_futureoption_functions as api
from backend.sessions.hours import time_range
from bot.clock import KST  # week02
from bot.journal import scrub  # week02
from bot.market import number  # week02
from trader import settings
from trader.strategy import Bar, Session, confirmed

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
SAMPLES_DIR = RESULTS_DIR / "samples"
MAX_PAGES = 10

DATE_FIELDS = ("occurs1_ref_dt", "occurs1_ko_dt", "occurs1_kor_dt", "occurs1_trd_dt", "occurs1_biz_dt", "occurs1_dt")
TIME_FIELDS = ("occurs1_exch_tm", "occurs1_ko_tm", "occurs1_kor_tm", "occurs1_trd_tm", "occurs1_tm")


def _first(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def parse_bars(raw: dict[str, Any], *, minutes: int = settings.BAR_MINUTES) -> list[Bar]:
    rows = raw.get("Occurs1")
    rows = rows if isinstance(rows, list) else []
    made: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ymd = _first(row, DATE_FIELDS)
        hms = _first(row, TIME_FIELDS).replace(":", "").zfill(6)
        try:
            start = datetime.strptime(ymd[:8] + hms[:6], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        prices = [number(row.get(f"occurs1_{k}_pric")) for k in ("open", "high", "low", "close")]
        if any(p is None or p <= 0 for p in prices):
            continue
        o, h, l, c = prices  # type: ignore[misc]
        volume = int(number(row.get("occurs1_cum_trd_qty")) or number(row.get("occurs1_trd_qty")) or 0)
        made.append(Bar(start, start + timedelta(minutes=minutes), o, h, l, c, volume))
    made.sort(key=lambda b: b.start)
    return made


def save_sample(name: str, raw: Any) -> Path | None:
    """응답 원문을 한 번만 남깁니다(필드 확인용). 이미 있으면 그대로 둡니다."""
    path = SAMPLES_DIR / f"{name}.json"
    if path.exists():
        return None
    try:
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scrub(raw), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except OSError:
        return None
    return path


def session_hours(*, exchange: str = settings.EXCHANGE, product: str = settings.PRODUCT) -> tuple[Any, Any]:
    """장운영정보의 1차 거래시간(한국시간). (open, close) time. 못 찾으면 ValueError."""
    raw = api.market_operations(exch_cd=exchange, prd_cd=product)
    save_sample("market_operations", raw)
    rows = raw.get("Occurs1") if isinstance(raw.get("Occurs1"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("occurs1_prd_cd") or "").strip() != product:
            continue
        found = time_range(row.get("occurs1_trd_tm_inf"))
        if found is not None and not found.is_point:
            return found.open, found.close
        raise ValueError(f"{product} 거래시간을 읽지 못했습니다: {row.get('occurs1_trd_tm_inf')!r}")
    raise ValueError(f"장운영정보에 {exchange} {product} 줄이 없습니다")


def session_for(now: datetime, hours: tuple[Any, Any], trade_end: Any = None) -> Session:
    """지금이 속한(또는 곧 열릴) 장. 자정을 넘는 장이면 어제 연 장이 아직 안 끝났을 수 있습니다."""
    today = now.date()
    yesterday = Session.of(today - timedelta(days=1), *hours, trade_end)
    if yesterday.open <= now < yesterday.close:
        return yesterday
    return Session.of(today, *hours, trade_end)


class OverseasMarket:
    def __init__(self, symbol: str = settings.SYMBOL, exch_cd: str = settings.EXCH_CD, *, feed: Any = None) -> None:
        self.symbol = symbol
        self.exch_cd = exch_cd
        self.feed = feed                 # trader.realtime.RealtimeFeed (없으면 REST 만)
        self.last_price_source = "rest"
        self._history: list[Bar] | None = None

    # ── 봉 ──────────────────────────────────────────────────

    def fetch_bars(self, first: date, last: date, minutes: int = settings.BAR_MINUTES) -> list[Bar]:
        """first~last 의 분봉 전부(진행 중 봉 포함) 시간순."""
        bars: dict[datetime, Bar] = {}
        next_key = api.FIRST_PAGE
        for _ in range(MAX_PAGES):
            raw = api.chart_minute(self.symbol, exch_cd=self.exch_cd,
                                   inq_strt_dt=f"{first:%Y%m%d}", inq_ed_dt=f"{last:%Y%m%d}",
                                   dcnt=str(minutes), next_key=next_key)
            save_sample(f"chart_minute_{minutes}m", raw)
            rows = raw.get("Occurs1") if isinstance(raw.get("Occurs1"), list) else []
            for bar in parse_bars(raw, minutes=minutes):
                bars.setdefault(bar.start, bar)
            key = str(raw.get("nxt_key") or "").strip()
            if not key or key == next_key or len(rows) < int(api.CHART_ROWS):
                break
            next_key = key
        return sorted(bars.values(), key=lambda b: b.start)

    def get_bars(self, session: Session, now: datetime) -> list[Bar]:
        """지표용 확정봉: 장 시작 전 봉(전날 봉으로 미리 채우기, 처음 한 번만 받음) + 이번 장. 시간순."""
        if self._history is None:
            first = session.open.date() - timedelta(days=settings.PRELOAD_DAYS)
            self._history = [b for b in self.fetch_bars(first, session.open.date()) if b.end <= session.open]
        today = [b for b in self.fetch_bars(session.open.date(), now.date()) if b.start >= session.open]
        return confirmed(self._history + today, now)

    # ── 현재가 ──────────────────────────────────────────────

    def rest_price(self) -> float:
        raw = api.price([(self.exch_cd, self.symbol)])
        rows = raw.get("Occurs1") if isinstance(raw.get("Occurs1"), list) else []
        value = number(rows[0].get("occurs1_last_pric")) if rows and isinstance(rows[0], dict) else None
        if value is None or value <= 0:
            raise ValueError(f"현재가를 읽지 못했습니다: {rows[:1]!r}")
        return value

    def get_price(self) -> float:
        """실시간 체결이 FEED_STALE_SEC 안에 왔으면 그 값, 아니면 REST 현재가."""
        tick = self.feed.latest() if self.feed is not None else None
        if tick is not None:
            age = datetime.now(tz=KST).timestamp() - tick.received.timestamp()
            if age <= settings.FEED_STALE_SEC:
                self.last_price_source = "ws"
                return tick.price
        self.last_price_source = "rest"
        return self.rest_price()

    def real_symbol(self) -> str:
        """실시간 구독에 쓰는 종목코드. 조회 코드와 다를 수 있습니다."""
        raw = api.price([(self.exch_cd, self.symbol)])
        save_sample("price", raw)
        rows = raw.get("Occurs1") if isinstance(raw.get("Occurs1"), list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        return str(row.get("occurs1_real_sym") or "").strip() or self.symbol


class HistoricalMarket:
    """리플레이: 받아 둔 봉을 가상 시계에 맞춰 내줍니다. 현재가는 확정된 마지막 1분봉 종가."""

    def __init__(self, bars_5m: list[Bar], bars_1m: list[Bar], clock: Any) -> None:
        self.bars_5m = bars_5m
        self.bars_1m = bars_1m
        self.clock = clock
        self.last_price_source = "replay-1m"

    def get_bars(self, session: Session, now: datetime) -> list[Bar]:
        return confirmed(self.bars_5m, now)

    def get_price(self) -> float:
        done = confirmed(self.bars_1m, self.clock.now())
        if not done:
            raise ValueError("이 시각까지 확정된 1분봉이 없습니다")
        return done[-1].close

