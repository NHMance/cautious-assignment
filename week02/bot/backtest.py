"""리플레이: 지난 하루를 API 로 받아 그 데이터로 봇을 돌립니다(`--replay-date`).

    uv run python -m bot.main --replay-date 20261028

- 시작할 때 그날 5분봉·1분봉을 **한 번만** 받아 `results/replay/bars_<종목>_<날짜>.json` 에 남깁니다.
- 시각은 가상 시계(08:55 → 15:20)로 흐르고, 봇은 그 시각까지 **확정된 봉만** 봅니다.
- 현재가는 그 시각까지 확정된 마지막 1분봉의 종가입니다. 봉 안의 고가·저가는 보지 않으므로
  손절·익절 판정은 1분 종가 기준입니다(실제보다 늦거나 놓칠 수 있음).
- 주문은 보내지 않습니다(DRY_RUN). 체결가는 주문 시점의 현재가로 가정합니다.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from bot import settings
from bot.broker import Broker
from bot.journal import RESULTS_DIR
from bot.market import Market
from bot.strategy import Bar, confirmed

REPLAY_DIR = RESULTS_DIR / "replay"

# 이 시각이 지나야 그날 봉이 다 모입니다(정규장 15:30 마감).
MARKET_CLOSE = time(15, 30)


def replay_day_problem(day: date, now: datetime) -> str | None:
    """그날로 리플레이할 수 없는 까닭. 돌려도 되면 None. API 를 부르기 전에 거릅니다.

    공휴일은 여기서 알 수 없어, 봉을 받아 본 뒤(0개면 휴장) 따로 알립니다.
    """
    today = now.date()
    if day > today:
        return f"{day} 은 아직 오지 않은 날입니다."
    if day.weekday() >= 5:
        return f"{day} 은 {'토일'[day.weekday() - 5]}요일이라 장이 열리지 않았습니다."
    if day == today and now.time() < MARKET_CLOSE:
        return f"오늘({day}) 장이 아직 끝나지 않았습니다. {MARKET_CLOSE:%H:%M} 이후에 돌리세요."
    return None


class HistoricalMarket:
    """받아 둔 하루치 봉을 가상 시계에 맞춰 내줍니다. 미래 봉은 보이지 않습니다."""

    def __init__(self, symbol: str, day: date, clock: Any, *, root: Path = REPLAY_DIR) -> None:
        self.symbol = symbol
        self.clock = clock
        market = Market(symbol)
        self.bars_5m = market.fetch_bars(day, settings.BAR_MINUTES)
        self.bars_1m = market.fetch_bars(day, 1)
        self.path = root / f"bars_{symbol}_{day:%Y%m%d}.json"
        # 봉이 없으면(휴장·종목 없음) 남기지 않습니다. 전에 받아 둔 봉 파일을 빈 파일로 덮지 않기 위해서입니다.
        if not self.bars_5m or not self.bars_1m:
            return
        root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"symbol": symbol, "day": day.isoformat(),
                        "bars_5m": [asdict(b) for b in self.bars_5m],
                        "bars_1m": [asdict(b) for b in self.bars_1m]},
                       ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )

    def get_bars_5m(self, today: date, now: datetime) -> list[Bar]:
        return confirmed(self.bars_5m, now)

    def get_price(self) -> float:
        done = confirmed(self.bars_1m, self.clock.now())
        if not done:
            raise ValueError("현재가를 읽지 못했습니다: 이 시각까지 확정된 1분봉이 없습니다")
        return done[-1].close


class SimBroker(Broker):
    """DRY_RUN 브로커에서 계좌 조회 두 개를 막습니다(지난 날짜라 지금 계좌를 볼 이유가 없음)."""

    def __init__(self, symbol: str, journal: Any, clock: Any) -> None:
        super().__init__(symbol, journal, clock, dry_run=True)

    def pending_count(self) -> int:
        return 0

    def orderable_qty(self, side: str, price: float) -> int:
        return settings.MAX_QTY
