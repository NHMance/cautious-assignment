"""읽기 전용 A/B 비교 대시보드. `results/` 의 journal 만 읽습니다(주문·계좌 조회 없음).

    uv sync --group dashboard
    uv run --group dashboard uvicorn frontend.app:app --port 8001   # → http://127.0.0.1:8001

기록 폴더 (trader/journal.py `root_for`)
    results/A, results/B                 실시간 (A 모의계좌 실주문)
    results/dry-run/A, results/dry-run/B 실시간 DRY_RUN
    results/replay/A, results/replay/B   리플레이

실시간 시세는 `/ws/live` 웹소켓으로 보냅니다(frontend/live.py). `DASHBOARD_LIVE=0` 이면 NH 에 접속하지 않습니다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from frontend.live import LiveHub

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
INDEX_HTML = Path(__file__).resolve().parent / "index.html"
MODES = {"live": RESULTS_DIR, "dry-run": RESULTS_DIR / "dry-run", "replay": RESULTS_DIR / "replay"}
LOG_LIMIT = 3000

hub = LiveHub()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await hub.shutdown()


app = FastAPI(title="week03 A/B 대시보드", docs_url=None, redoc_url=None, lifespan=lifespan)


def read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            break  # 쓰는 도중인 마지막 줄
    return out


def _running(records: list[dict[str, Any]]) -> bool:
    last = max((i for i, r in enumerate(records) if r.get("type") == "RUN_START"), default=-1)
    return last >= 0 and not any(r.get("type") == "RUN_END" for r in records[last:])


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in records if r.get("type") == "TRADE_CLOSED"]
    pnls = [r.get("pnl_usd") or 0 for r in closed if r.get("pnl_usd") is not None]
    start = next((r for r in reversed(records) if r.get("type") == "RUN_START"), {})
    end = next((r for r in reversed(records) if r.get("type") == "RUN_END"), None)
    slips = [r["slippage_ticks"] for r in closed if r.get("slippage_ticks") is not None]
    return {
        "exists": bool(records),
        "running": _running(records),
        "environment": start.get("environment"),
        "dry_run": start.get("dry_run"),
        "session": start.get("session"),
        "trades": len(closed),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "pnl_usd": round(sum(pnls), 2),
        "ticks": round(sum(r.get("ticks") or 0 for r in closed), 2),
        "avg_slippage_ticks": round(sum(slips) / len(slips), 2) if slips else None,
        "filter_blocks": sum(1 for r in records if r.get("type") == "ATR_FILTER_BLOCK"),
        "skips": sum(1 for r in records if r.get("type") == "SKIP"),
        "errors": sum(1 for r in records if r.get("type") == "ERROR"),
        "shared": sum(1 for r in closed if r.get("fill_source") == "A"),
        "assumed": sum(1 for r in closed if r.get("fill_source") == "assumed"),
        "stop_reason": (end or {}).get("stop_reason_text") or (end or {}).get("stop_reason") or (end or {}).get("source_stop_reason"),
        "state": records[-1].get("state") if records else None,
        "last_ts": records[-1].get("ts") if records else None,
    }


@app.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    """실시간 시세(FA)를 브라우저로 밀어 줍니다. 브라우저가 붙어 있을 때만 NH 에 접속합니다."""
    await ws.accept()
    q = await hub.join()

    async def pump() -> None:
        while True:
            await ws.send_json(await q.get())

    sender: asyncio.Task | None = None
    try:
        await ws.send_json(hub.snapshot())
        sender = asyncio.create_task(pump())
        # 브라우저가 닫히면 바로 disconnect 가 옵니다. 그때 허브에서 빠집니다.
        while (await ws.receive()).get("type") != "websocket.disconnect":
            pass
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        if sender is not None:
            sender.cancel()
        await hub.leave(q)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/days")
def days() -> list[dict[str, Any]]:
    out = []
    for mode, base in MODES.items():
        found: set[str] = set()
        for variant in ("A", "B"):
            found |= {p.stem.removeprefix("journal_") for p in (base / variant).glob("journal_*.jsonl")}
        for day in sorted(found, reverse=True):
            out.append({"mode": mode, "day": day,
                        "A": (base / "A" / f"journal_{day}.jsonl").is_file(),
                        "B": (base / "B" / f"journal_{day}.jsonl").is_file()})
    return out


@app.get("/api/compare")
def compare(mode: str = Query(...), day: str = Query(..., pattern=r"^\d{8}$")) -> dict[str, Any]:
    base = MODES.get(mode)
    if base is None:
        raise HTTPException(404, "모르는 모드")
    a = read_lines(base / "A" / f"journal_{day}.jsonl")
    b = read_lines(base / "B" / f"journal_{day}.jsonl")
    if not a and not b:
        raise HTTPException(404, "기록 없음")
    start = next((r for r in a if r.get("type") == "RUN_START"), {})
    session_open = (start.get("session") or {}).get("open", "")
    bars = [{k: r.get(k) for k in ("bar", "ema_fast", "ema_slow", "atr", "atr_ratio", "cross")}
            for r in a if r.get("type") == "BAR" and (r.get("bar") or {}).get("start", "") >= session_open]
    # 같은 봉이 두 번 기록될 수 있어(재시작) 뒤엣것만 남깁니다
    bars = list({x["bar"]["start"]: x for x in bars}.values())

    def trades(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in records if r.get("type") == "TRADE_CLOSED"]

    def opens(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"ts": r.get("ts"), "fill_source": r.get("fill_source"), **(r.get("trade") or {})}
                for r in records if r.get("type") == "ENTRY_OPEN"]

    def logs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 미리 채우기용 지난 봉은 빼고, 화면의 「봉 숨기기」로 거를 수 있게 장중 봉은 함께 보냅니다.
        out = [r for r in records
               if r.get("type") != "BAR" or (r.get("bar") or {}).get("start", "") >= session_open]
        return out[-LOG_LIMIT:]

    return {
        "mode": mode, "day": day, "bars": bars,
        "A": {"summary": _summary(a), "trades": trades(a), "opens": opens(a), "logs": logs(a)},
        "B": {"summary": _summary(b), "trades": trades(b), "opens": opens(b), "logs": logs(b),
              "blocks": [r for r in b if r.get("type") == "ATR_FILTER_BLOCK"]},
    }
