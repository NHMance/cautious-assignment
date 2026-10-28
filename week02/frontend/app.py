"""읽기 전용 대시보드. `results/` 의 저널(JSONL)만 읽어 localhost 화면에 보여 줍니다.

    uv sync --group dashboard
    uv run --group dashboard uvicorn frontend.app:app --port 8000   # → http://127.0.0.1:8000

- 실시간 모의 트레이딩 기록: `results/journal_YYYYMMDD.jsonl` (`RUN_START.environment == "paper"`),
  주문 없이 돈 장중 DRY_RUN 은 `results/dry-run/` (같은 실시간 탭에 DRY_RUN 으로 표시)
- 리플레이 기록: `results/replay/journal_YYYYMMDD.jsonl` (`--replay-date`, `environment == "replay"`)
- 봇 테스트(`--bot-test`, `results/bot-test/`)·단위 테스트 기록은 전략 결과가 아니라 흐름 점검용이라 보여 주지 않습니다.
- 주문·계좌 조회는 하지 않습니다. 봇은 터미널에서 따로 돌리고, 화면은 기록을 따라 읽기만 합니다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
INDEX_HTML = Path(__file__).resolve().parent / "index.html"

# environment 값 → 화면의 모드. 여기 없는 environment(bot-test·test)의 기록은 화면에 내지 않습니다.
# "sim" 은 --replay-date 의 옛 이름(10/28 기록)입니다.
MODES = {"paper": "live", "replay": "replay", "sim": "replay"}

app = FastAPI(title="week02 대시보드", docs_url=None, redoc_url=None)


def _journal_files() -> dict[str, Path]:
    """화면에 보여 줄 수 있는 저널만 모읍니다. 키(`replay/journal_….jsonl`)가 곧 요청 이름입니다."""
    files: dict[str, Path] = {}
    paths = [*sorted(RESULTS_DIR.glob("journal_*.jsonl")),
             *sorted((RESULTS_DIR / "dry-run").glob("journal_*.jsonl")),
             *sorted((RESULTS_DIR / "replay").glob("journal_*.jsonl"))]
    for path in paths:
        files[path.relative_to(RESULTS_DIR).as_posix()] = path
    return files


def _read_lines(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # 봇이 줄을 쓰는 도중에 읽으면 마지막 줄이 잘려 있을 수 있습니다. 다음 조회에서 다시 읽습니다.
            break
    return records


def _describe(name: str, path: Path) -> dict[str, Any]:
    records = _read_lines(path)
    starts = [r for r in records if r.get("type") == "RUN_START"]
    # RUN_START 가 아직 없으면(막 만들어진 파일) 폴더로 짐작합니다: results/replay/ 는 replay, results/ 는 paper.
    environment = starts[-1].get("environment", "") if starts else ("replay" if name.startswith("replay/") else "paper")
    # 마지막 RUN_START 뒤에 RUN_END 가 없으면 아직 도는 중입니다.
    last_start = max((i for i, r in enumerate(records) if r.get("type") == "RUN_START"), default=-1)
    running = last_start >= 0 and not any(r.get("type") == "RUN_END" for r in records[last_start:])
    return {
        "name": name,
        "day": path.stem.removeprefix("journal_"),
        "environment": environment,
        "mode": MODES.get(environment),
        "symbol": records[0].get("symbol", "") if records else "",
        "events": len(records),
        "last_ts": records[-1].get("ts") if records else None,
        "running": running,
        "dry_run": bool(starts[-1].get("dry_run")) if starts else None,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/journals")
def journals() -> list[dict[str, Any]]:
    described = (_describe(name, path) for name, path in _journal_files().items())
    return [meta for meta in described if meta["mode"]]


@app.get("/api/journal")
def journal(file: str = Query(...), after: int = Query(0, ge=0)) -> dict[str, Any]:
    """`after` 번째 줄부터 돌려줍니다. 화면은 받은 만큼 `after` 를 늘려 새 줄만 이어 받습니다."""
    path = _journal_files().get(file)
    meta = _describe(file, path) if path else None
    if meta is None or not meta["mode"]:
        raise HTTPException(status_code=404, detail="없는 기록입니다")
    records = _read_lines(path)
    return {"meta": meta, "total": len(records), "events": records[after:]}
