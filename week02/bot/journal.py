"""JSONL 기록. `results/journal_YYYYMMDD.jsonl` 에 한 줄 한 이벤트.

공통 필드: `ts`(KST ISO), `type`, `symbol`, `state`. 당일 파일이 있으면 이어 씁니다.
인증 정보·계좌번호는 남기지 않습니다 — 응답 원문을 적기 전에 그런 칸을 지웁니다.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bot.clock import RealClock

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"

# 이 글자가 들어간 칸은 값을 지웁니다(계좌번호·계좌명·주문자 ID·접속 IP·인증 정보).
_SECRET_PARTS = ("ac_no", "ac_nm", "acnt", "ord_id", "user_ip", "app_key", "app_secret", "secret", "token", "password")
MASKED = "***"


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: MASKED if any(part in str(key).lower() for part in _SECRET_PARTS) else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


class Journal:
    def __init__(
        self,
        symbol: str,
        *,
        clock: Any | None = None,
        state: Callable[[], str] | None = None,
        root: Path = RESULTS_DIR,
        echo: bool = True,
    ) -> None:
        self.symbol = symbol
        self.clock = clock or RealClock()
        self.state = state or (lambda: "")
        self.root = root
        self.echo = echo

    def path(self) -> Path:
        return self.root / f"journal_{self.clock.now():%Y%m%d}.jsonl"

    def write(self, type_: str, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": self.clock.now().isoformat(timespec="seconds"),
            "type": type_,
            "symbol": self.symbol,
            "state": self.state(),
            **scrub(fields),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.echo and type_ != "BAR":
            brief = {k: v for k, v in record.items() if k not in ("ts", "symbol", "settings", "response", "bars")}
            print(f"[{record['ts'][11:19]}] {json.dumps(brief, ensure_ascii=False, default=str)}", flush=True)
        return record

    def error(self, api: str, exc: BaseException, **fields: Any) -> dict[str, Any]:
        """API 오류는 메시지 코드·내용 그대로."""
        return self.write(
            "ERROR",
            api=api,
            kind=type(exc).__name__,
            code=getattr(exc, "code", None) or getattr(exc, "status", None),
            message=getattr(exc, "detail", None) or str(exc),
            **fields,
        )

    def today(self) -> list[dict[str, Any]]:
        """오늘 파일에 이미 적힌 기록. 재시작할 때 진입 횟수를 되살리는 데 씁니다."""
        path = self.path()
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records
