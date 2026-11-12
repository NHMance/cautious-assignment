from __future__ import annotations
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import re
from .strategy import KST

PRIVATE = re.compile(r"ac_no|acnt|account|ac_nm|ord_id|user_ip|app.?key|secret|token|password|authorization", re.I)


def clean(value):
    if is_dataclass(value):
        return clean(asdict(value))
    if isinstance(value, dict):
        return {k: "***" if PRIVATE.search(str(k)) else clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (datetime, Path)):
        return str(value)
    return value


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(clean(value), f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


class Journal:
    def __init__(self, root: Path, mode: str, *, echo=False, day=None):
        self.root, self.mode, self.echo = root, mode, echo
        root.mkdir(parents=True, exist_ok=True)
        self.day = day or datetime.now(KST).date()
        self.path = root / f"journal_{self.day:%Y%m%d}.jsonl"

    def write(self, event: str, *, ts=None, **values):
        record = clean({"ts": ts or datetime.now(KST), "type": event, "mode": self.mode, **values})
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if self.echo and event in {"RUN_START", "ENTRY_ATTEMPT", "ORDER_INTENT", "ORDER_SUBMIT", "FILL",
                                   "PAIR_OPEN", "PAIR_CLOSED", "UNWIND", "HALT", "RUN_END", "SKIP",
                                   "QUOTE_RETRY", "QUOTE_RETRY_EXHAUSTED", "WS_STATUS", "REFERENCE_UPDATE", "REFERENCE_ERROR"}:
            brief = {k: record[k] for k in ("role", "reason", "order_no", "net_pnl", "flat",
                                          "error_kind", "api", "symbol", "cause", "detail", "manual_reconciliation",
                                          "attempt", "max_retries", "delay_seconds", "status") if k in record}
            print(f"[{record['ts']}] {self.mode} {event} {json.dumps(brief, ensure_ascii=False)}", flush=True)
        return record
