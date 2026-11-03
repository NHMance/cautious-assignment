"""week02 Journal 에서 파일 이름의 날짜만 고정합니다.

week02 는 `지금` 날짜로 파일을 고릅니다. MKU 장이 자정을 넘으면 한 장의 기록이 두 파일로 갈라지므로,
**장 시작일**로 이름을 고정합니다. 나머지(기록 모양, 비밀 칸 지우기)는 week02 그대로입니다.

주문·체결·진입·청산 기록은 재시작 복구의 근거라, 적은 뒤 `fsync` 로 디스크까지 내립니다
(전원이 꺼져도 마지막 거래 기록이 남게). 봉·시세 기록은 자주 오므로 내리지 않습니다.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import trader  # noqa: F401  (week02 경로 등록)
from bot.journal import MASKED, Journal  # week02

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"

# week02 의 지우는 칸에 더해: 주문내역의 사용자 ID·접속 IP, 웹소켓 O1 의 작업자(사용자 ID) 칸.
# (11/03 모의 주문 왕복 점검에서 실제 응답에 이 칸들이 그대로 오는 것을 확인)
EXTRA_SECRET_PARTS = ("user_id", "ip_addr", "work_empl")


def scrub_extra(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: MASKED if any(part in str(key).lower() for part in EXTRA_SECRET_PARTS) else scrub_extra(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_extra(item) for item in value]
    return value


DURABLE_TYPES = frozenset({"ORDER_SUBMIT", "ORDER_CANCEL", "FILL", "ENTRY_OPEN", "TRADE_CLOSED", "RECOVER", "RUN_END"})


class DayJournal(Journal):
    def __init__(self, symbol: str, day: date, **kwargs: Any) -> None:
        super().__init__(symbol, **kwargs)
        self.day = day

    def path(self) -> Path:
        return self.root / f"journal_{self.day:%Y%m%d}.jsonl"

    def write(self, type_: str, **fields: Any) -> dict[str, Any]:
        record = super().write(type_, **scrub_extra(fields))
        if type_ in DURABLE_TYPES:
            try:
                fd = os.open(self.path(), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass  # 줄은 이미 적혔습니다. 디스크 동기화 실패만으로 거래를 멈추지 않습니다.
        return record


def root_for(variant: str, mode: str) -> Path:
    """기록 폴더. mode: live(모의계좌 실주문) · dry-run · replay · bot-test."""
    base = RESULTS_DIR if mode == "live" else RESULTS_DIR / mode
    return base / variant
