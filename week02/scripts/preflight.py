"""장 시작 전 점검 (10/28 08:30 이전). 모의 인증 정보가 설정되어 있어야 합니다.

    uv run python scripts/preflight.py

1. 토큰 발급 — 첫 조회가 성공하면 발급된 것입니다.
2. 현재가·호가 10단계 → 이웃한 호가 간격의 최솟값으로 실제 호가단위 확인, settings.tick_size() 와 비교.
3. 예수금·주문가능수량 — 증거금 체크 근거 (화면 캡처해 두기).
4. 잔고·미체결 — 0 이어야 합니다.
결과는 results/preflight_YYYYMMDD.json 에도 남깁니다(계좌번호 칸은 지움).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.domestic_futureoption import domestic_futureoption_functions as api  # noqa: E402
from backend.config import ConfigError  # noqa: E402
from backend.nh_api import runtime  # noqa: E402
from backend.nh_api.errors import NHApiError  # noqa: E402
from backend.nh_api.session import create_session  # noqa: E402
from bot import credentials, settings  # noqa: E402
from bot.clock import RealClock  # noqa: E402
from bot.journal import RESULTS_DIR, scrub  # noqa: E402
from bot.market import number  # noqa: E402


def step(title: str, fn):
    print(f"\n== {title}")
    try:
        value = fn()
    except NHApiError as exc:
        print(f"  실패: {exc}")
        return {"error": str(exc), "code": getattr(exc, "code", None)}
    return value


def main() -> int:
    sym = settings.SYMBOL
    try:
        config, source = credentials.load()
    except ConfigError as exc:
        print(exc)
        return 2
    session = create_session(config=config)
    runtime.use(session)
    env = session.config.default_environment
    print(f"환경: {env}, 종목: {sym}, 인증 정보: {source}")
    if env != "paper":
        print("⚠ 기본 환경이 paper 가 아닙니다. 설정 파일을 확인하세요.")
    today = RealClock().now().strftime("%Y%m%d")
    report: dict = {"environment": env, "symbol": sym}

    price_raw = step("현재가", lambda: api.price(sym))
    last = number(price_raw.get("last_pric")) if "error" not in price_raw else None
    print(f"  현재가 {last}, 승수(응답) {price_raw.get('mltplr')}")
    report["price"] = {k: price_raw.get(k) for k in ("last_pric", "mltplr", "pd_sett_pric", "ref_pric", "error")}

    bid_raw = step("호가 10단계 → 호가단위", lambda: api.price_bid(sym))
    levels = set()
    for row in bid_raw.get("Occurs1") or []:
        for key in ("occurs1_ask_best_pric", "occurs1_bid_best_pric"):
            value = number(row.get(key))
            if value:
                levels.add(value)
    ordered = sorted(levels)
    gaps = sorted({round(b - a, 6) for a, b in zip(ordered, ordered[1:]) if b > a})
    observed = gaps[0] if gaps else None
    expected = settings.tick_size(last) if last else None
    print(f"  호가 {len(ordered)}개, 간격 {gaps[:5]} → 관측 호가단위 {observed}, settings.tick_size()={expected}")
    if observed and expected and observed != expected:
        print("  ⚠ 다릅니다! bot/settings.py 의 TICK_TABLE 을 고치세요.")
    report["tick"] = {"levels": ordered, "observed": observed, "settings": expected}

    dep = step("예수금", lambda: api.deposit(today))
    report["deposit"] = {k: dep.get(k) for k in ("dps_aset_tot_amt", "dps_csh_amt", "ord_psb_tot_amt", "ord_psb_csh_amt", "error")}
    print(f"  {report['deposit']}")

    if last:
        for side, name in (("1", "매수"), ("2", "매도")):
            oq = step(f"주문가능수량({name})", lambda side=side: api.orderable_quantity(sym, trd_div=side, ord_pric=f"{last:g}"))
            report[f"orderable_{name}"] = {k: oq.get(k) for k in ("ord_psb_qty", "tot_psb_qty", "ord_psb_amt", "error")}
            print(f"  {report[f'orderable_{name}']}")
        print(f"  필요 증거금 추정: {last} × {settings.MULTIPLIER} × {settings.INIT_MARGIN_RATE} = {last * settings.MULTIPLIER * settings.INIT_MARGIN_RATE:,.0f}")

    oi = step("잔고", lambda: api.open_interest())
    report["open_interest"] = [{k: r.get(k) for k in ("occurs1_sym", "occurs1_trd_div", "occurs1_opint_qty", "occurs1_avg_pric")}
                               for r in oi.get("Occurs1") or []]
    print(f"  {report['open_interest'] or '없음'}")

    pend = step("미체결", lambda: api.order_history(exec_div=api.PENDING))
    report["pending"] = len(pend.get("Occurs1") or [])
    print(f"  {report['pending']}건")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"preflight_{today}.json"
    out.write_text(json.dumps(scrub(report), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {out}")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
