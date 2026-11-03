"""해외 실시간(웹소켓) 접속 점검. 주문은 내지 않습니다.

    uv run python scripts/ws_check.py                    # MKUX26 FA 체결 30초 수신
    uv run python scripts/ws_check.py --seconds 120 --fb --o1
    uv run python scripts/ws_check.py --symbol MKUZ26

1. week03/.env 의 해외 모의 키로 토큰 → REST 현재가로 종목·실시간 종목코드(`occurs1_real_sym`) 확인
2. 웹소켓 키 발급 → 접속 → session_init → FA(·FB·O1) 구독
3. 받은 메시지를 화면에 찍고, 원문 앞 50건을 `results/ws_samples/` 에 남깁니다(계좌번호 등은 지움).
   명세에 응답 예시가 없어 실제 메시지 모양(값이 body 안인지)을 이 파일로 확인합니다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trader  # noqa: E402,F401
from backend.config import ConfigError  # noqa: E402
from backend.nh_api import runtime  # noqa: E402
from backend.nh_api.errors import NHApiError  # noqa: E402
from backend.nh_api.session import create_session  # noqa: E402
from backend.overseas_futureoption import overseas_futureoption_functions as api  # noqa: E402
from bot.clock import KST  # noqa: E402
from bot.journal import scrub  # noqa: E402
from trader import credentials  # noqa: E402
from trader.realtime import FA, FB, O1, NHStream, RealtimeFeed, Subscription, parse_tick, session_credentials  # noqa: E402

SAMPLES_DIR = ROOT / "results" / "ws_samples"
KEEP_RAW = 50


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MKUX26")
    ap.add_argument("--exch", default="FSGX", help="거래소코드 (선물은 F+거래소)")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--fb", action="store_true", help="시세호가 FB 도 구독")
    ap.add_argument("--o1", action="store_true", help="주문/체결 O1 도 구독")
    args = ap.parse_args()

    try:
        config, source = credentials.load()
    except ConfigError as exc:
        print(exc)
        return 2
    print(f"인증 정보: {source} · REST {config.environment('paper').base_url} · WS {credentials.ws_url()}")
    session = create_session(config=config)
    runtime.use(session)

    real_sym = args.symbol
    try:
        raw = api.price([(args.exch, args.symbol)])
        row = (raw.get("Occurs1") or [{}])[0]
        real_sym = str(row.get("occurs1_real_sym") or "").strip() or args.symbol
        print(f"REST 현재가: {row.get('occurs1_sym_nm')} last={row.get('occurs1_last_pric')} "
              f"time={row.get('occurs1_trd_tm')} real_sym={real_sym}")
    except NHApiError as exc:
        print(f"REST 현재가 조회 실패 (웹소켓 점검은 계속): {exc}")

    subs = [Subscription(FA, real_sym)]
    if args.fb:
        subs.append(Subscription(FB, real_sym))
    if args.o1:
        subs.append(Subscription(O1))

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    sample_path = SAMPLES_DIR / f"ws_{args.symbol}_{datetime.now(tz=KST):%Y%m%d_%H%M%S}.jsonl"
    counts: dict[str, int] = {}
    kept = 0

    def keep(kind: str, msg: dict) -> None:
        nonlocal kept
        if kept >= KEEP_RAW:
            return
        kept += 1
        with sample_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "msg": scrub(msg)}, ensure_ascii=False) + "\n")

    def on_status(kind: str, detail: dict) -> None:
        print(f"[{datetime.now(tz=KST):%H:%M:%S}] {kind} {json.dumps(scrub(detail), ensure_ascii=False)[:300]}")
        if kind not in ("subscribe_sent", "reconnect_wait"):
            keep(kind, detail)

    def on_data(code: str, msg: dict) -> None:
        counts[code or "?"] = counts.get(code or "?", 0) + 1
        keep(f"data:{code}", msg)
        tick = parse_tick(msg) if code == FA else None
        if tick:
            print(f"  FA {tick.sym} {tick.price} x{tick.qty} 체결 {tick.ko_exec_tm} 매수/매도호가 {tick.bid}/{tick.ask}")
        elif counts[code or "?"] <= 3:
            print(f"  {code or '?'} {json.dumps(scrub(msg), ensure_ascii=False)[:300]}")

    feed = RealtimeFeed(
        lambda data, status: NHStream(credentials.ws_url(), session_credentials(session), subs, data, status),
        listeners=[on_data], on_status=on_status,
    )
    feed.start()
    ok = False
    deadline = time.time() + 20
    while time.time() < deadline and feed.alive() and not ok:
        ok = feed.ready.wait(timeout=0.2)
    if ok:
        time.sleep(args.seconds)
    feed.stop()
    session.close()

    print(f"\n세션 초기화: {'성공' if ok else '실패'} · 데이터 메시지 {counts or '없음'} · 원문 {kept}건 → {sample_path}")
    if ok and not counts.get(FA):
        print("FA 체결이 없었습니다. 장 시간인지, 종목코드(실시간 종목코드)가 맞는지 확인하세요.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
