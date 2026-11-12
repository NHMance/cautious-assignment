"""통계 갱신과 실시간 호가 판정을 분리합니다. 확정봉 조회는 수신을 막지 않습니다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from queue import SimpleQueue, Empty
from statistics import mean, pstdev
import threading

from .market import fetch_bars, align
from .strategy import KST, Strategy, Signal


@dataclass(frozen=True)
class Reference:
    end: datetime | None
    count: int
    mean: float = 0
    std: float = 0

    def problem(self, now, cfg):
        if self.count < cfg.window or self.end is None:
            return "WARMUP"
        if int(self.end.timestamp()) != (int(now.timestamp()) // 300) * 300:
            return "STALE_REFERENCE"
        if self.std <= 1e-8:
            return "ZERO_STD"
        return None


def quote_signal(reference, nq, fq, now, cfg):
    """현재 호가 중간값으로 신호와 진입 조건을 함께 계산합니다."""
    if reference is None:
        return None, "REFERENCE_PENDING", {}
    problem = reference.problem(now, cfg)
    if problem:
        return None, problem, {"reference_end": reference.end, "count": reference.count}
    spread = fq.mid - nq.mid
    z = (spread - reference.mean) / reference.std
    stats = dict(mean=reference.mean, std=reference.std, spread=spread, z=z,
                 reference_end=reference.end, source="KB_midpoint")
    if abs(z) + 1e-8 >= cfg.stop_z:
        return None, "BEYOND_STOP_BAND", stats
    if abs(z) + 1e-8 < cfg.entry_z:
        return None, "NO_SIGNAL", stats
    return Signal(now, -1 if z > 0 else 1, reference.mean, reference.std, z, spread), "SIGNAL", stats


class BarReference:
    """직전 24개 확정 5분봉 통계를 별도 스레드에서 갱신합니다."""
    def __init__(self, client, symbols, cfg):
        self.client, self.symbols, self.cfg = client, tuple(symbols), cfg
        self.events = SimpleQueue()
        self._lock = threading.Lock()
        self._reference = None
        self._stop = threading.Event()
        self._thread = None

    def write(self, event, **values):
        # fetch_bars의 journal로 쓰되, 파일 기록은 메인 스레드에서 처리합니다.
        if self._stop.is_set():
            raise InterruptedError("통계 갱신 종료")
        self.events.put((event, values))

    def latest(self):
        with self._lock:
            return self._reference

    def drain_events(self):
        while True:
            try:
                yield self.events.get_nowait()
            except Empty:
                return

    def refresh(self, now):
        nb = fetch_bars(self.client, self.symbols[0], now.date(), self)
        fb = fetch_bars(self.client, self.symbols[1], now.date(), self)
        now = datetime.now(KST)
        pairs, missing = align(nb, fb, now)
        strategy = Strategy(self.cfg)
        for pair in pairs:
            strategy.observe(pair)
        window = list(strategy.window)
        reference = Reference(strategy.last, len(window), mean(window) if window else 0,
                              pstdev(window) if window else 0)
        with self._lock:
            self._reference = reference
        self.write("REFERENCE_UPDATE", reference=reference, missing=missing,
                   reason=reference.problem(now, self.cfg) or "READY")
        return reference

    def start(self):
        def run():
            while not self._stop.is_set():
                now = datetime.now(KST)
                try:
                    reference = self.refresh(now)
                    now = datetime.now(KST)
                    # 새 확정봉이 아직 없으면 재확인하며, 호가 판정은 기다리지 않습니다.
                    current = reference.end and int(reference.end.timestamp()) == (int(now.timestamp()) // 300) * 300
                    delay = max(0.1, ((int(now.timestamp()) // 300) + 1) * 300 - now.timestamp()) if current else 1
                except Exception as exc:
                    if self._stop.is_set():
                        return
                    with self._lock:
                        self._reference = None
                    self.write("REFERENCE_ERROR", error_kind=type(exc).__name__, api=getattr(exc, "api_id", None))
                    delay = 2
                self._stop.wait(delay)
        self._thread = threading.Thread(target=run, name="nh-bar-reference", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=25)
