"""해외 주문·체결 확인·잔고 확인. 시장가 1계약만 냅니다.

필드 (week02 backend 의 해외 계좌 파서·라우트에서 확인)
- 주문 `overseas_order`: 요청 `qt_div_cd`(1 신규)·`trd_div_cd`(1 매수/2 매도)·`glb_ord_tp_cd`(1 시장가)·
  `ord_pric`("0", 시장가)·`stop_pric`("")·`org_ord_no`("0"). 응답 `ord_no`, 거부면 `err_cd`·`err_msg`.
- 주문내역 `overseas_order-history` 줄: `occurs1_ord_no`·`occurs1_sym`·`occurs1_trd_tp`·`occurs1_ord_qty`·
  `occurs1_exec_qty`·`occurs1_exec_pric`·`occurs1_ord_avg_pric`·`occurs1_ord_rem_qty`·`occurs1_ord_sts`.
  미체결은 `inq_div="3"`.
- 잔고 `overseas_open-interest` 줄: `occurs1_sym`·`occurs1_trd_tp`(1 매수/2 매도)·`occurs1_opint_qty`·`occurs1_avg_pric`.
- 주문가능수량: 응답에 양방향이 함께 옵니다(`buy_ord_psb_qty`·`sel_ord_psb_qty`).

week02 와 같은 원칙: **응답을 못 받은 주문은 다시 보내지 않고** 주문내역에서 접수 여부부터 찾습니다.
접수만으로는 체결로 보지 않습니다. 신규/청산 구분이 없어 보유 반대 방향 주문이 청산입니다.

예외 구분 (주문 한 건 = 접수 → 체결 확인 → 잔고 확인 을 한 묶음으로 봅니다)
- 서버가 오류로 **답한** 경우(`NHApiError` 중 `TransportError` 가 아닌 것, `err_cd`): 접수 안 됨이 확실 → `None`.
- 응답을 **못 받았거나** 응답을 읽다 예상 못한 예외가 난 경우: 접수됐을 수도 있음 → 다시 보내지 않고
  주문내역에서 찾습니다. 못 찾으면 `ORDER_UNKNOWN` 을 남기고 `None` — 봇이 다음 봉에서 잔고로 맞춰 봅니다.
- 조회(주문내역·취소) 중 예외는 기록만 하고 다음 확인으로 넘어갑니다. 예외 하나로 체결 확인 묶음이 끊기지 않게 합니다.
"""
from __future__ import annotations

from typing import Any

import trader  # noqa: F401  (week02 경로 등록)
from backend.nh_api.errors import NHApiError, TransportError
from backend.overseas_futureoption import overseas_futureoption_functions as api
from bot.broker import Fill, Position  # week02
from bot.journal import Journal  # week02
from bot.market import number  # week02
from trader import settings
from trader.strategy import LONG, SHORT

BUY, SELL = "1", "2"


def trd_code(side: str) -> str:
    return BUY if side == LONG else SELL


def side_of(code: Any) -> str:
    return LONG if str(code).strip() == BUY else SHORT


def opposite(side: str) -> str:
    return SHORT if side == LONG else LONG


def _ord_no(value: Any) -> str:
    return str(value or "").strip().lstrip("0")


def _rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = raw.get("Occurs1")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _rejected(code: Any) -> bool:
    text = str(code or "").strip()
    return bool(text) and set(text) != {"0"}


class OverseasBroker:
    def __init__(self, symbol: str, journal: Journal, clock: Any, *, dry_run: bool = False) -> None:
        self.symbol = symbol
        self.journal = journal
        self.clock = clock
        self.dry_run = dry_run
        self._dry_seq = 0
        self._dry_position: Position | None = None

    # ── 조회 ────────────────────────────────────────────────

    def position(self) -> Position | None:
        if self.dry_run:
            return self._dry_position
        net, cost = 0, 0.0
        for row in _rows(api.open_interest()):
            if str(row.get("occurs1_sym") or "").strip() != self.symbol:
                continue
            qty = int(number(row.get("occurs1_opint_qty")) or 0)
            if qty <= 0:
                continue
            net += qty if side_of(row.get("occurs1_trd_tp")) == LONG else -qty
            cost += (number(row.get("occurs1_avg_pric")) or 0.0) * qty
        if net == 0:
            return None
        return Position(LONG if net > 0 else SHORT, abs(net), cost / abs(net) if cost else None)

    def history(self, inq_div: str = api.ALL) -> list[dict[str, Any]]:
        raw = api.order_history(inq_div=inq_div)
        return [r for r in _rows(raw) if str(r.get("occurs1_sym") or "").strip() == self.symbol]

    def pending_count(self) -> int:
        if self.dry_run:
            return 0
        return sum(1 for r in self.history(api.PENDING) if (number(r.get("occurs1_ord_rem_qty")) or 0) > 0)

    def orderable_qty(self, side: str, price: float) -> int | None:
        if self.dry_run:
            return settings.MAX_QTY
        raw = api.orderable_quantity(self.symbol, glb_ord_tp_cd=api.MARKET_PRICE, ord_pric=f"{price:g}")
        value = number(raw.get("buy_ord_psb_qty" if side == LONG else "sel_ord_psb_qty"))
        return int(value) if value is not None else None

    # ── 주문 ────────────────────────────────────────────────

    def submit(self, side: str, qty: int, *, role: str) -> str | None:
        request = {"sym": self.symbol, "qt_div_cd": api.NEW, "trd_div_cd": trd_code(side),
                   "glb_ord_tp_cd": api.MARKET_PRICE, "ord_qty": str(qty), "ord_pric": "0", "stop_pric": ""}
        if self.dry_run:
            self._dry_seq += 1
            order_no = f"DRY{self._dry_seq:04d}"
            self.journal.write("ORDER_SUBMIT", role=role, side=side, request=request, order_no=order_no, dry_run=True)
            return order_no

        before = self._known_orders()
        try:
            response = api.order(self.symbol, qt_div_cd=api.NEW, trd_div_cd=request["trd_div_cd"],
                                 glb_ord_tp_cd=api.MARKET_PRICE, ord_qty=request["ord_qty"],
                                 ord_pric="0", stop_pric="")
            if not isinstance(response, dict):
                raise ValueError(f"주문 응답 모양이 다릅니다: {type(response).__name__}")
        except TransportError as exc:
            return self._recover_unknown(exc, before, side, qty, role, request)
        except NHApiError as exc:
            # 서버가 오류로 답했습니다 → 접수 안 됨.
            self.journal.error("overseas_order", exc, role=role, request=request)
            return None
        except Exception as exc:  # noqa: BLE001  — 보냈는지 모르는 상태와 같게 다룹니다
            return self._recover_unknown(exc, before, side, qty, role, request)

        if _rejected(response.get("err_cd")):
            self.journal.write("ERROR", api="overseas_order", code=response.get("err_cd"),
                               message=response.get("err_msg") or "주문 거부", role=role, request=request)
            return None
        order_no = _ord_no(response.get("ord_no"))
        self.journal.write("ORDER_SUBMIT", role=role, side=side, request=request, response=response,
                           order_no=order_no or None)
        if not order_no:
            self.journal.write("ERROR", api="overseas_order", code="NO_ORDER_NO", message="주문번호 없음", role=role)
            return None
        return order_no

    def wait_fill(self, order_no: str, *, side: str, qty: int, ref_price: float | None,
                  role: str, timeout: float = settings.FILL_TIMEOUT_SEC) -> Fill | None:
        if self.dry_run:
            if ref_price is None:
                return None
            pos = self._dry_position
            if pos is None:
                self._dry_position = Position(side, qty, ref_price)
            elif pos.side != side:
                left = pos.qty - qty
                self._dry_position = Position(pos.side, left, pos.avg_price) if left > 0 else None
            self.journal.write("FILL", role=role, order_no=order_no, side=side, price=ref_price, qty=qty,
                               dry_run=True, fill_source="assumed", note="DRY_RUN: 현재가를 체결가로 가정")
            return Fill(order_no, ref_price, qty)

        deadline = self.clock.now().timestamp() + timeout
        rows: list[dict[str, Any]] = []
        fill: Fill | None = None
        while True:
            try:
                rows = [r for r in self.history() if _ord_no(r.get("occurs1_ord_no")) == order_no]
            except Exception as exc:  # noqa: BLE001
                self.journal.error("overseas_order-history", exc, order_no=order_no)
                rows = []
            fill = self._fill_from(order_no, rows)
            if fill is not None and fill.qty >= qty:
                self.journal.write("FILL", role=role, order_no=order_no, side=side, price=fill.price, qty=fill.qty,
                                   fill_source="actual", status=rows[0].get("occurs1_ord_sts") if rows else None)
                return fill
            if self.clock.now().timestamp() >= deadline:
                break
            self.clock.sleep(settings.FILL_POLL_SEC)

        filled = fill.qty if fill else 0
        self.journal.write("ERROR", api="overseas_order-history", code="FILL_TIMEOUT",
                           message=f"{timeout:g}초 안에 체결 확인 실패 (체결 {filled}/{qty})", order_no=order_no, rows=rows)
        if qty - filled > 0:
            self.cancel(order_no, side=side, qty=qty - filled)
        if fill is not None and fill.qty > 0:
            self.journal.write("FILL", role=role, order_no=order_no, side=side, price=fill.price, qty=fill.qty,
                               fill_source="actual", partial=True)
            return fill
        return None

    def cancel(self, order_no: str, *, side: str, qty: int) -> None:
        try:
            response = api.order(self.symbol, qt_div_cd=api.CANCEL, trd_div_cd=trd_code(side),
                                 glb_ord_tp_cd=api.MARKET_PRICE, ord_qty=str(qty), ord_pric="0",
                                 stop_pric="", org_ord_no=order_no.zfill(10))
            self.journal.write("ORDER_CANCEL", order_no=order_no, qty=qty, response=response)
        except Exception as exc:  # noqa: BLE001  — 취소 실패는 미체결 확인(NO_PENDING)·잔고 맞춤으로 다시 잡힙니다
            self.journal.error("overseas_order(cancel)", exc, order_no=order_no)

    def cancel_pending(self) -> int:
        """이 종목 미체결을 모두 취소 요청합니다. 요청한 건수를 돌려줍니다(조회 실패면 0)."""
        if self.dry_run:
            return 0
        try:
            rows = self.history(api.PENDING)
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_order-history", exc, note="미체결 취소 전 조회")
            return 0
        count = 0
        for row in rows:
            remaining = int(number(row.get("occurs1_ord_rem_qty")) or 0)
            order_no = _ord_no(row.get("occurs1_ord_no"))
            if remaining > 0 and order_no:
                self.cancel(order_no, side=side_of(row.get("occurs1_trd_tp")), qty=remaining)
                count += 1
        return count

    # ── 내부 ────────────────────────────────────────────────

    @staticmethod
    def _fill_from(order_no: str, rows: list[dict[str, Any]]) -> Fill | None:
        total, value = 0, 0.0
        for row in rows:
            qty = int(number(row.get("occurs1_exec_qty")) or 0)
            price = number(row.get("occurs1_exec_pric")) or number(row.get("occurs1_ord_avg_pric")) or 0.0
            if qty > 0 and price > 0:
                total += qty
                value += price * qty
        return Fill(order_no, value / total, total) if total else None

    def _recover_unknown(self, exc: BaseException, before: set[str] | None, side: str, qty: int,
                         role: str, request: dict[str, Any]) -> str | None:
        """주문을 보냈는지 모를 때: 다시 보내지 않고 주문내역에서 새 주문을 찾습니다."""
        self.journal.error("overseas_order", exc, role=role, request=request, note="응답 없음 — 접수 여부 확인")
        found = self._find_new_order(before, side, qty)
        self.journal.write("ORDER_SUBMIT", role=role, side=side, request=request, response=None,
                           order_no=found, recovered_from_timeout=True)
        if found is None:
            self.journal.write("ERROR", api="overseas_order", code="ORDER_UNKNOWN", role=role, side=side,
                               message="접수 여부를 확인하지 못했습니다 — 다시 보내지 않고 잔고로 맞춰 봅니다")
        return found

    def _known_orders(self) -> set[str] | None:
        try:
            return {_ord_no(r.get("occurs1_ord_no")) for r in self.history()}
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_order-history", exc, note="주문 전 내역 조회")
            return None

    def _find_new_order(self, before: set[str] | None, side: str, qty: int) -> str | None:
        if before is None:
            return None
        for _ in range(3):
            self.clock.sleep(settings.FILL_POLL_SEC)
            try:
                rows = self.history()
            except Exception as exc:  # noqa: BLE001
                self.journal.error("overseas_order-history", exc, note="시간 초과 뒤 접수 확인")
                continue
            for row in rows:
                no = _ord_no(row.get("occurs1_ord_no"))
                if no and no not in before and side_of(row.get("occurs1_trd_tp")) == side \
                        and int(number(row.get("occurs1_ord_qty")) or 0) == qty:
                    return no
        return None
