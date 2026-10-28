"""주문·체결 확인·잔고 확인.

필드는 저장된 모의 응답(`domestic_order`·`domestic_order-history`·`domestic_open-interest`)에서
확인했습니다.
- 주문 응답: `ord_no`, 성공이면 `msg_cd`·`msg_cntn` 이 빈 문자열.
- 주문내역 줄: `occurs1_ord_no`·`occurs1_sym`·`occurs1_trd_div`·`occurs1_ord_qty`·
  `occurs1_exec_qty`·`occurs1_exec_pric`·`occurs1_avg_exec_pric`·`occurs1_rem_ord_qty`·
  `occurs1_exch_exec_no`·`occurs1_ord_sts_nm`. 시장가 체결 줄의 `avg_exec_pric` 가 0 으로
  온 기록이 있어 `exec_pric` 를 먼저 봅니다.
- 잔고 줄: `occurs1_sym`·`occurs1_trd_div`(1 매수, 2 매도)·`occurs1_opint_qty`·`occurs1_avg_pric`.

**신규/청산 구분이 없습니다.** 보유 반대 방향 주문이 청산입니다.
**응답 시간 초과 시 같은 주문을 다시 보내지 않습니다** — 주문내역으로 접수 여부부터 확인합니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.domestic_futureoption import domestic_futureoption_functions as api
from backend.nh_api.errors import NHApiError, TransportError
from bot import settings
from bot.journal import Journal
from bot.market import number
from bot.strategy import LONG, SHORT

BUY = "1"
SELL = "2"


def trd_div(side: str) -> str:
    return BUY if side == LONG else SELL


def side_of(trd: str) -> str:
    return LONG if str(trd).strip() == BUY else SHORT


def opposite(side: str) -> str:
    return SHORT if side == LONG else LONG


def _ord_no(value: Any) -> str:
    return str(value or "").strip().lstrip("0")


def _rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = raw.get("Occurs1")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


@dataclass(frozen=True)
class Position:
    side: str
    qty: int
    avg_price: float | None


@dataclass(frozen=True)
class Fill:
    order_no: str
    price: float
    qty: int


class Broker:
    def __init__(self, symbol: str, journal: Journal, clock: Any, *, dry_run: bool = settings.DRY_RUN) -> None:
        self.symbol = symbol
        self.journal = journal
        self.clock = clock
        self.dry_run = dry_run
        self._dry_seq = 0
        self._dry_position: Position | None = None
        self._dry_orders: dict[str, tuple[str, int]] = {}

    # ── 조회 ────────────────────────────────────────────────

    def position(self) -> Position | None:
        """해당 종목 순보유. 없으면 None. 주간·야간 줄이 섞여 와서 종목으로 거릅니다."""
        if self.dry_run:
            return self._dry_position
        raw = api.open_interest()
        net = 0
        cost = 0.0
        for row in _rows(raw):
            if str(row.get("occurs1_sym") or "").strip() != self.symbol:
                continue
            qty = int(number(row.get("occurs1_opint_qty")) or 0)
            if qty <= 0:
                continue
            sign = 1 if side_of(row.get("occurs1_trd_div")) == LONG else -1
            net += sign * qty
            cost += (number(row.get("occurs1_avg_pric")) or 0.0) * qty
        if net == 0:
            return None
        return Position(LONG if net > 0 else SHORT, abs(net), cost / abs(net) if cost else None)

    def history(self, exec_div: str = api.ALL) -> list[dict[str, Any]]:
        raw = api.order_history(exec_div=exec_div, trd_tm_div_cd=api.DAY_SESSION)
        return [r for r in _rows(raw) if str(r.get("occurs1_sym") or "").strip() == self.symbol]

    def pending_count(self) -> int:
        rows = self.history(api.PENDING)
        return sum(1 for r in rows if (number(r.get("occurs1_rem_ord_qty")) or 0) > 0) if rows else 0

    def orderable_qty(self, side: str, price: float) -> int | None:
        raw = api.orderable_quantity(self.symbol, trd_div=trd_div(side), ord_pric=f"{price:g}")
        value = number(raw.get("ord_psb_qty"))
        if value is None:
            value = number(raw.get("tot_psb_qty"))
        return int(value) if value is not None else None

    # ── 주문 ────────────────────────────────────────────────

    def submit(self, side: str, qty: int, *, role: str) -> str | None:
        """시장가 주문. 주문번호를 돌려줍니다. 실패하면 None (사유는 ERROR 로 기록)."""
        request = {
            "sym": self.symbol,
            "trd_div": trd_div(side),
            "ord_tp_cd": api.MARKET_PRICE,
            "ord_qty": str(qty),
            "ord_pric": "0",
            "exec_cnd_cd": api.FAS,
        }
        if self.dry_run:
            self._dry_seq += 1
            order_no = f"DRY{self._dry_seq:04d}"
            self._dry_orders[order_no] = (side, qty)
            self.journal.write("ORDER_SUBMIT", role=role, side=side, request=request, response=None,
                               order_no=order_no, dry_run=True)
            return order_no

        before = self._known_orders()
        try:
            response = api.order(
                self.symbol,
                trd_div=request["trd_div"],
                ord_tp_cd=request["ord_tp_cd"],
                ord_qty=request["ord_qty"],
                ord_pric=request["ord_pric"],
                exec_cnd_cd=request["exec_cnd_cd"],
            )
        except TransportError as exc:
            # 접수됐는지 모릅니다. 다시 보내지 않고 주문내역에서 새로 생긴 주문을 찾습니다.
            self.journal.error("domestic_order", exc, role=role, request=request)
            found = self._find_new_order(before, side, qty)
            self.journal.write("ORDER_SUBMIT", role=role, side=side, request=request, response=None,
                               order_no=found, recovered_from_timeout=True)
            return found
        except NHApiError as exc:
            self.journal.error("domestic_order", exc, role=role, request=request)
            return None

        order_no = _ord_no(response.get("ord_no"))
        self.journal.write("ORDER_SUBMIT", role=role, side=side, request=request, response=response,
                           order_no=order_no or None)
        if not order_no:
            self.journal.write("ERROR", api="domestic_order", code=response.get("msg_cd"),
                               message=response.get("msg_cntn") or "주문번호 없음", role=role)
            return None
        return order_no

    def wait_fill(self, order_no: str, *, side: str, qty: int, ref_price: float | None,
                  role: str, timeout: float = settings.FILL_TIMEOUT_SEC) -> Fill | None:
        """주문내역을 조회해 체결가·체결수량을 확보합니다. 접수만으로는 체결로 보지 않습니다."""
        if self.dry_run:
            if ref_price is None:
                return None
            fill = Fill(order_no, ref_price, qty)
            pos = self._dry_position
            if pos is None:
                self._dry_position = Position(side, qty, ref_price)
            elif pos.side != side:
                left = pos.qty - qty
                self._dry_position = Position(pos.side, left, pos.avg_price) if left > 0 else None
            self.journal.write("FILL", role=role, order_no=order_no, side=side, price=fill.price, qty=fill.qty,
                               dry_run=True, note="DRY_RUN: 현재가를 체결가로 가정")
            return fill

        deadline = self.clock.now().timestamp() + timeout
        rows: list[dict[str, Any]] = []
        while True:
            try:
                rows = [r for r in self.history() if _ord_no(r.get("occurs1_ord_no")) == order_no]
            except NHApiError as exc:
                self.journal.error("domestic_order-history", exc, order_no=order_no)
                rows = []
            fill = self._fill_from(order_no, rows)
            if fill is not None and fill.qty >= qty:
                self.journal.write("FILL", role=role, order_no=order_no, side=side, price=fill.price, qty=fill.qty,
                                   status=rows[0].get("occurs1_ord_sts_nm") if rows else None)
                return fill
            if self.clock.now().timestamp() >= deadline:
                break
            self.clock.sleep(settings.FILL_POLL_SEC)

        # 시간 안에 다 체결되지 않았습니다. 남은 수량을 취소하고 기록합니다.
        filled = fill.qty if fill else 0
        self.journal.write("ERROR", api="domestic_order-history", code="FILL_TIMEOUT",
                           message=f"{timeout:g}초 안에 체결 확인 실패 (체결 {filled}/{qty})",
                           order_no=order_no, rows=rows)
        remaining = qty - filled
        if remaining > 0:
            self.cancel(order_no, side=side, qty=remaining)
        if fill is not None and fill.qty > 0:
            self.journal.write("FILL", role=role, order_no=order_no, side=side, price=fill.price, qty=fill.qty,
                               partial=True)
            return fill
        return None

    def cancel(self, order_no: str, *, side: str, qty: int) -> None:
        try:
            response = api.cancel(self.symbol, org_ord_no=order_no.zfill(10), trd_div=trd_div(side), ord_qty=str(qty))
            self.journal.write("ORDER_CANCEL", order_no=order_no, qty=qty, response=response)
        except NHApiError as exc:
            self.journal.error("domestic_cancel", exc, order_no=order_no)

    # ── 내부 ────────────────────────────────────────────────

    @staticmethod
    def _fill_from(order_no: str, rows: list[dict[str, Any]]) -> Fill | None:
        """같은 주문의 체결 줄을 체결번호별로 합칩니다."""
        fills: dict[str, tuple[float, int]] = {}
        for row in rows:
            qty = int(number(row.get("occurs1_exec_qty")) or 0)
            if qty <= 0:
                continue
            price = number(row.get("occurs1_exec_pric")) or number(row.get("occurs1_avg_exec_pric")) or 0.0
            if price <= 0:
                continue
            key = str(row.get("occurs1_exch_exec_no") or len(fills))
            fills[key] = (price, qty)
        total = sum(q for _, q in fills.values())
        if total <= 0:
            return None
        avg = sum(p * q for p, q in fills.values()) / total
        return Fill(order_no, avg, total)

    def _known_orders(self) -> set[str] | None:
        try:
            return {_ord_no(r.get("occurs1_ord_no")) for r in self.history()}
        except NHApiError as exc:
            self.journal.error("domestic_order-history", exc, note="주문 전 내역 조회")
            return None

    def _find_new_order(self, before: set[str] | None, side: str, qty: int) -> str | None:
        if before is None:
            # 주문 전 목록이 없으면 옛 주문을 새 주문으로 잘못 고를 수 있습니다. 잔고 확인에 맡깁니다.
            return None
        for _ in range(3):
            self.clock.sleep(settings.FILL_POLL_SEC)
            try:
                rows = self.history()
            except NHApiError as exc:
                self.journal.error("domestic_order-history", exc, note="시간 초과 뒤 접수 확인")
                continue
            for row in rows:
                no = _ord_no(row.get("occurs1_ord_no"))
                if no in before:
                    continue
                if side_of(row.get("occurs1_trd_div")) == side and int(number(row.get("occurs1_ord_qty")) or 0) == qty:
                    return no
        return None
