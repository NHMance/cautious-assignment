"""진입 직전 리스크 체크. 하나라도 실패하면 진입하지 않고 사유를 기록합니다.

값을 모으는 일(API 조회)은 `main.py` 가 하고, 여기서는 판정만 합니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot import settings


@dataclass(frozen=True)
class RiskInput:
    price: float
    tick: float
    orderable_qty: int | None      # orderable_quantity 의 ord_psb_qty. 조회 실패면 None
    position_qty: int              # open_interest 의 해당 종목 보유 수량
    pending_orders: int            # order_history(exec_div="2") 의 해당 종목 미체결 건수
    entries_today: int


def evaluate(x: RiskInput) -> list[dict[str, Any]]:
    """실패한 항목 목록. 비어 있으면 통과."""
    failures: list[dict[str, Any]] = []

    def check(name: str, ok: bool, value: Any, limit: Any) -> None:
        if not ok:
            failures.append({"check": name, "value": value, "limit": limit})

    planned_loss = settings.STOP_TICKS * x.tick * settings.MULTIPLIER + settings.EST_ROUNDTRIP_COST
    check("1_MAX_LOSS", planned_loss <= settings.MAX_LOSS_PER_TRADE, planned_loss, settings.MAX_LOSS_PER_TRADE)

    notional = x.price * settings.MULTIPLIER * settings.MAX_QTY
    check("2_NOTIONAL", notional <= settings.INITIAL_CAPITAL, notional, settings.INITIAL_CAPITAL)

    margin = round(notional * settings.INIT_MARGIN_RATE)
    check(
        "3_MARGIN",
        x.orderable_qty is not None and x.orderable_qty >= settings.MAX_QTY,
        {"orderable_qty": x.orderable_qty, "required_margin": margin},
        settings.MAX_QTY,
    )
    check("4_NO_POSITION", x.position_qty == 0, x.position_qty, 0)
    check("5_NO_PENDING", x.pending_orders == 0, x.pending_orders, 0)
    check("6_ENTRIES", x.entries_today < settings.MAX_ENTRIES_PER_DAY, x.entries_today, settings.MAX_ENTRIES_PER_DAY)
    return failures
