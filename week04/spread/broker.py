"""각 레그 주문을 확인한 뒤 다음 레그를 보냅니다. 접수 불명 주문은 재전송하지 않습니다."""
from __future__ import annotations
from datetime import datetime
from math import isfinite
import time
from .strategy import Fill, KST, MULTIPLIER, Position


class Uncertain(RuntimeError):
    """주문/잔고 상태를 확정할 수 없습니다. 자동 추가 주문을 막고 대사를 요구합니다."""


def order_id(value):
    return str(value or "").strip().lstrip("0")


class PaperBroker:
    simulated = False

    def __init__(self, client, symbols, journal, timeout=10):
        from backend.domestic_futureoption import domestic_futureoption_functions as nh
        self.nh, self.client = nh, client
        self.symbols, self.journal, self.timeout = tuple(symbols), journal, timeout
        if client.session.config.default_environment != "paper" or client.session.is_replay:
            raise ValueError("모의 서버의 실제 세션만 주문할 수 있습니다")
        if client.session.base_url("paper").rstrip("/") != "https://apidemo.futures.co.kr":
            raise ValueError("모의 주문 서버 주소 불일치")

    def _pages(self, method, name, **kwargs):
        key, seen, rows = "", set(), []
        for _ in range(100):
            raw = method(next_key=key, api=self.client, **kwargs)
            self.journal.write("API_RESPONSE", api=name, response=raw)
            page = raw.get("Occurs1")
            if not isinstance(page, list):
                # 정상적인 0건 응답만 허용합니다. 누락을 빈 계좌로 해석하지 않습니다.
                if not str(raw.get("nrec", "")).strip().isdigit() or int(raw["nrec"]) != 0:
                    raise Uncertain(f"{name}: 내역 필드 누락")
                page = []
            if not page and str(raw.get("nrec", "")).strip().isdigit() and int(raw["nrec"]) > 0:
                raise Uncertain(f"{name}: 건수는 있으나 내역이 비어 있습니다")
            rows.extend(page)
            nxt = str(raw.get("nxt_key") or "").strip()
            if not nxt:
                return rows
            if nxt in seen or nxt == key:
                raise Uncertain(f"{name}: 연속조회 키 반복")
            seen.add(nxt)
            key = nxt
        raise Uncertain(f"{name}: 연속조회 상한 초과")

    def history(self, pending=False):
        return self._pages(self.nh.order_history, "domestic_order-history",
                           exec_div=self.nh.PENDING if pending else self.nh.ALL,
                           trd_tm_div_cd=self.nh.DAY_SESSION)

    def snapshot(self):
        positions = {s: 0 for s in self.symbols}
        for r in self._pages(self.nh.open_interest, "domestic_open-interest"):
            s = str(r.get("occurs1_sym") or "").strip()
            if s not in positions:
                continue
            side = str(r.get("occurs1_trd_div") or "").strip()
            qty = int(float(r["occurs1_opint_qty"]))
            if qty < 0:
                raise Uncertain("잔고수량 부호 해석 실패")
            if qty and side not in ("1", "2"):
                raise Uncertain("잔고 매매구분 해석 실패")
            # 양방향 잔고를 상계하여 평평하다고 오판하지 않도록 행별로 검사합니다.
            if qty and positions[s]:
                raise Uncertain("대상 월물의 잔고 행이 중복됩니다. 계좌 대사가 필요합니다")
            positions[s] += qty * (1 if side == "1" else -1)
        pending = []
        for r in self.history(pending=True):
            if str(r.get("occurs1_sym") or "").strip() not in positions:
                continue
            if "occurs1_rem_ord_qty" not in r:
                raise Uncertain("대상 미체결의 잔량 필드 누락")
            if float(r["occurs1_rem_ord_qty"]) > 0:
                pending.append(r)
        self.journal.write("ACCOUNT_SNAPSHOT", positions=positions, pending=pending)
        return positions, pending

    def assert_positions(self, expected):
        try:
            actual, pending = self.snapshot()
        except Uncertain:
            raise
        except Exception as exc:
            raise Uncertain("잔고·미체결 조회 실패: 추가 주문 전 대사 필요") from exc
        if actual != expected or pending:
            raise Uncertain(f"잔고·미체결 불일치: 예상 {expected}, 실제 {actual}, 미체결 {len(pending)}")

    def _fill(self, no, symbol, side, rows):
        executions = {}
        for r in rows:
            if order_id(r.get("occurs1_ord_no")) != no:
                continue
            if str(r.get("occurs1_sym", "")).strip() != symbol:
                raise Uncertain("주문번호에 연결된 종목 불일치")
            if str(r.get("occurs1_trd_div", "")).strip() != ("1" if side == 1 else "2"):
                raise Uncertain("주문번호에 연결된 매매구분 불일치")
            qty = int(float(r.get("occurs1_exec_qty") or 0))
            if not qty:
                continue
            price = float(r.get("occurs1_exec_pric") or 0) or float(r.get("occurs1_avg_exec_pric") or 0)
            exec_no = str(r.get("occurs1_exch_exec_no") or "").strip()
            if qty != 1 or not isfinite(price) or price <= 0 or not exec_no:
                raise Uncertain("1계약 체결수량·가격·거래소 체결번호 검증 실패")
            if exec_no in executions and executions[exec_no] != price:
                raise Uncertain("동일 체결번호의 가격이 서로 다릅니다")
            executions[exec_no] = price
        if len(executions) > 1:
            raise Uncertain("1계약 주문에 복수 체결번호: 중복 체결 대사 필요")
        return Fill(symbol, side, next(iter(executions.values())), datetime.now(KST), no) if executions else None

    def execute(self, symbol, side, quote, role):
        if symbol not in self.symbols or side not in (-1, 1):
            raise ValueError("허용된 두 월물·매수/매도 방향만 주문할 수 있습니다")
        try:
            return self._execute(symbol, side, quote, role)
        except Uncertain:
            raise
        except Exception as exc:
            raise Uncertain("주문·체결·취소 확인 중 오류: 추가 주문 전 대사 필요") from exc

    def _execute(self, symbol, side, quote, role):
        from backend.nh_api.errors import BusinessError
        request = dict(sym=symbol, trd_div="1" if side == 1 else "2", ord_tp_cd=self.nh.MARKET_PRICE,
                       ord_qty="1", ord_pric="0", exec_cnd_cd=self.nh.FAS)
        self.journal.write("ORDER_INTENT", role=role, request=request, reference_price=quote.price(side))
        try:
            response = self.nh.order(**request, api=self.client)
        except BusinessError as exc:
            self.journal.write("ORDER_REJECTED", role=role, symbol=symbol, code=exc.code)
            return None
        except Exception as exc:
            self.journal.write("ORDER_UNKNOWN", role=role, symbol=symbol, error_kind=type(exc).__name__)
            # 조회 결과를 기록하되, 지연 접수 가능성 때문에 새 주문을 보내지 않습니다.
            try:
                self.history()
                self.snapshot()
            except Exception:
                pass
            raise Uncertain("접수 여부 불명: 재주문·반대주문을 중단하고 HTS에서 대사하세요") from exc
        no = order_id(response.get("ord_no"))
        self.journal.write("ORDER_SUBMIT", role=role, response=response, order_no=no)
        if not no:
            raise Uncertain("주문번호 누락: 접수 여부 대사 필요")
        end = time.monotonic() + self.timeout
        while True:
            fill = self._fill(no, symbol, side, self.history())
            if fill:
                self.journal.write("FILL", role=role, fill=fill, reference_price=quote.price(side),
                                   adverse_slippage=(fill.price - quote.price(side)) * side)
                return fill
            if time.monotonic() >= end:
                break
            time.sleep(1)
        # 취소 접수와 취소 완료는 다릅니다. 취소 후에도 체결 여부를 재확인합니다.
        self.journal.write("CANCEL_INTENT", order_no=no, symbol=symbol)
        response = self.nh.cancel(symbol, org_ord_no=no.zfill(10), trd_div=request["trd_div"], ord_qty="1", api=self.client)
        self.journal.write("CANCEL_SUBMIT", order_no=no, response=response)
        for _ in range(5):
            time.sleep(1)
            rows = self.history()
            fill = self._fill(no, symbol, side, rows)
            if fill:
                self.journal.write("FILL", role=role, fill=fill, after_cancel=True)
                return fill
            own = [r for r in rows if order_id(r.get("occurs1_ord_no")) == no]
            pending = [r for r in self.history(pending=True) if order_id(r.get("occurs1_ord_no")) == no]
            if own and not pending and any("취소" in str(r.get("occurs1_ord_sts_nm", "")) for r in own):
                self.journal.write("CANCEL_CONFIRMED", order_no=no)
                return None
        raise Uncertain("취소 완료·최종 체결 여부를 확정하지 못했습니다")


class SimBroker:
    simulated = True

    def __init__(self, symbols, journal):
        self.positions = {s: 0 for s in symbols}
        self.journal = journal
        self.seq = 0

    def snapshot(self):
        return dict(self.positions), []

    def assert_positions(self, expected):
        if self.positions != expected:
            raise Uncertain("시뮬레이션 장부 불일치")

    def execute(self, symbol, side, quote, role):
        self.seq += 1
        fill = Fill(symbol, side, quote.price(side), quote.ts, f"SIM{self.seq:05d}")
        self.journal.write("ORDER_INTENT", ts=quote.ts, role=role, symbol=symbol, side=side)
        self.positions[symbol] += side
        self.journal.write("FILL", ts=quote.ts, role=role, fill=fill, simulated=True)
        return fill


class PairExecutor:
    """각 체결을 장부와 대조하고, 알려진 단독 체결은 반대매매로 정리합니다."""
    def __init__(self, broker, near, far, journal, cfg):
        self.broker, self.near, self.far = broker, near, far
        self.journal, self.cfg = journal, cfg
        self.position = None
        self.closing = {}
        self.realized = 0.0
        self.entries = 0
        self.halted = False

    def expected(self):
        out = {self.near: 0, self.far: 0}
        if self.position:
            for f in (self.position.near, self.position.far):
                if f.symbol not in self.closing:
                    out[f.symbol] = f.side
        return out

    def open(self, signal, nq, fq):
        if self.halted or self.position:
            raise RuntimeError("진입할 수 없는 상태입니다")
        self.broker.assert_positions({self.near: 0, self.far: 0})
        self.entries += 1  # 실패한 시도도 일일 최대 3회에 포함합니다.
        self.journal.write("ENTRY_ATTEMPT", ts=nq.ts, signal=signal, entry_number=self.entries)
        # 근월물 체결을 확인한 후 차월물을 보냅니다. 두 주문은 원자적이지 않습니다.
        n = self.broker.execute(self.near, -signal.direction, nq, "ENTRY_NEAR")
        if n is None:
            self.broker.assert_positions({self.near: 0, self.far: 0})
            self.journal.write("ENTRY_ABORTED", ts=nq.ts, reason="NEAR_NOT_FILLED")
            return False
        self.broker.assert_positions({self.near: n.side, self.far: 0})
        f = self.broker.execute(self.far, signal.direction, fq, "ENTRY_FAR")
        if f is None:
            self.halted = True
            self.broker.assert_positions({self.near: n.side, self.far: 0})
            # 모의계좌에서는 시장가 반대주문. 시뮬레이션에서는 같은 호가를 사용합니다.
            u = self.broker.execute(self.near, -n.side, nq, "UNWIND_NEAR")
            if u is None:
                raise Uncertain("차월물 진입 실패 후 근월물 정리 실패: 단독 포지션 확인 필요")
            self.broker.assert_positions({self.near: 0, self.far: 0})
            gross = round(n.side * (u.price - n.price) * MULTIPLIER, 6)
            fees = self.cfg.fee_per_leg_side * 2
            pnl = gross - fees
            self.realized += pnl
            self.journal.write("UNWIND", ts=u.ts, near_pnl=pnl, net_pnl=pnl,
                               gross_pnl=gross, fees=fees, entry=n, exit=u, halted=True)
            return False
        self.position = Position(signal, n, f)
        self.broker.assert_positions(self.expected())
        self.journal.write("PAIR_OPEN", ts=f.ts, position=self.position,
                           leg_gap_seconds=(f.ts - n.ts).total_seconds(),
                           expected_spread=signal.spread, actual_spread=f.price - n.price)
        return True

    def close(self, nq, fq, reason):
        p = self.position
        if not p:
            return True
        self.broker.assert_positions(self.expected())
        for entry, quote in ((p.near, nq), (p.far, fq)):
            if entry.symbol in self.closing:
                continue
            fill = self.broker.execute(entry.symbol, -entry.side, quote, "EXIT_" + entry.symbol)
            if fill is None:
                self.journal.write("EXIT_PENDING", ts=quote.ts, symbol=entry.symbol, reason=reason)
                return False
            self.closing[entry.symbol] = fill
            self.broker.assert_positions(self.expected())
        n, f = self.closing[self.near], self.closing[self.far]
        np = round(p.near.side * (n.price - p.near.price) * MULTIPLIER, 6)
        fp = round(p.far.side * (f.price - p.far.price) * MULTIPLIER, 6)
        fee = self.cfg.fee_per_leg_side * 4
        net = np + fp - fee
        self.realized += net
        self.journal.write("PAIR_CLOSED", ts=f.ts, reason=reason, near_pnl=np, far_pnl=fp,
                           gross_pnl=np + fp, fees=fee, net_pnl=net, realized=self.realized,
                           hold_minutes=(f.ts - p.entered_at).total_seconds() / 60,
                           entry=p, exit_near=n, exit_far=f)
        self.position = None
        self.closing.clear()
        return True
