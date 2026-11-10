"""국내 모의 API 연결, 월물 검증, 호가·확정봉 조회와 CSV 입출력."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import os
import json
from .strategy import ROOT, KST, Quote, MULTIPLIER, Bar, PairBar, at
import hashlib
from .journal import save_json
from math import isfinite


def create_client(env_file: Path | None = None):
    environment = "paper"
    from backend.config import Config, EnvironmentConfig, MarketConfig, DEFAULT_BASE_URLS, load_config
    from backend.nh_api.session import create_session
    from backend.nh_api.client import TradeClient
    from bot.credentials import load_env_file
    load_env_file(env_file or ROOT / ".env")
    prefix = "NH_PAPER"
    key, secret = os.getenv(prefix + "_APP_KEY", "").strip(), os.getenv(prefix + "_APP_SECRET", "").strip()
    if bool(key) != bool(secret):
        raise ValueError(f"{prefix}_APP_KEY 와 APP_SECRET 을 모두 입력하세요")
    if key:
        env = EnvironmentConfig(environment, DEFAULT_BASE_URLS[environment],
                                MarketConfig(environment, "domestic", key, secret),
                                MarketConfig(environment, "overseas", "", ""))
        config = Config(default_environment=environment, environments={environment: env})
    else:
        source = load_config()
        config = replace(source, default_environment=environment)
    # 사용자 설정의 base_url 이 실전으로 바뀌어도 모의 주문 경로가 바뀌지 않게 고정합니다.
    selected = replace(config.environment(environment), base_url=DEFAULT_BASE_URLS[environment])
    if not selected.domestic.app_key or not selected.domestic.app_secret:
        raise ValueError(f"{environment}.domestic 인증 정보가 없습니다")
    config = replace(config, environments={environment: selected})
    return TradeClient(create_session(config=config, replay=False))


def quote(api, symbol, journal):
    from backend.domestic_futureoption import domestic_futureoption_functions as nh
    raw = nh.price_bid(symbol, api=api)
    now = datetime.now(KST)
    journal.write("API_RESPONSE", api="domestic_price-bid", symbol=symbol, response=raw)
    if str(raw.get("sym", "")).strip() != symbol:
        raise ValueError("호가 응답 종목 불일치/누락")
    tm = str(raw.get("qt_tm", "")).strip().replace(":", "").zfill(6)
    ts = datetime.strptime(now.strftime("%Y%m%d") + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    asks, bids = [], []
    for r in raw.get("Occurs1", []):
        a, b = float(r["occurs1_ask_best_pric"]), float(r["occurs1_bid_best_pric"])
        aq, bq = int(float(r["occurs1_ask_qty"])), int(float(r["occurs1_bid_qty"]))
        if a > 0:
            asks.append((a, aq))
        if b > 0:
            bids.append((b, bq))
    if not asks or not bids:
        raise ValueError("최우선 양방향 호가가 없습니다")
    ask, aq = min(asks)
    bid, bq = max(bids)
    return Quote(symbol, ts, bid, ask, bq, aq)


def verify_prices(api, pair, journal):
    from backend.domestic_futureoption import domestic_futureoption_functions as nh
    today = datetime.now(KST).date()
    for key in ("near", "far"):
        symbol = pair[key]["series"]
        raw = nh.price(symbol, api=api)
        journal.write("API_RESPONSE", api="domestic_price", symbol=symbol, response=raw)
        if str(raw.get("sym_exch", "")).strip() != symbol:
            raise ValueError("현재가 응답 종목 불일치")
        expiry = str(raw.get("lst_trd_dt", "")).strip().replace("-", "")
        if not expiry.startswith("202611" if key == "near" else "202612"):
            raise ValueError("현재가 최종거래일이 설계한 월물과 다릅니다")
        if today >= datetime.strptime(expiry, "%Y%m%d").date():
            raise ValueError("최종거래일 또는 만기 경과 월물은 실행하지 않습니다")
        if float(raw.get("mltplr") or 0) != MULTIPLIER:
            raise ValueError("거래승수 불일치")


def load_pair(path=ROOT / "instruments.json"):
    pair = json.loads(path.read_text(encoding="utf-8"))
    for name, month in (("near", "202611"), ("far", "202612")):
        r = pair[name]
        if (r["under_name"] != "USD" or r["fo_tp"] != "F" or r["opt_tp"] != "1"
                or r["mrkt_tp"] != "6" or r["cme_tp"] != "day"
                or float(r["ctrt_size"]) != MULTIPLIER or not r["exp_ym"].startswith(month)):
            raise ValueError("미국달러선물 2026년 11·12월물 주간 설정을 확인하세요")
    if not pair["near"]["series"] or not pair["far"]["series"] or pair["near"]["series"] == pair["far"]["series"]:
        raise ValueError("서로 다른 두 월물 코드가 필요합니다")
    return pair


def validate_bar(bar):
    if bar.start.tzinfo is None or bar.start.utcoffset() != at(bar.start.date(), "09:00").utcoffset():
        raise ValueError("봉 시각은 한국시간(+09:00)이어야 합니다")
    if bar.start.second or bar.start.microsecond or bar.start.minute % 5:
        raise ValueError("5분 경계가 아닌 봉 시각입니다")
    if not all(isfinite(p) and p > 0 for p in (bar.open, bar.close)) or bar.volume < 0:
        raise ValueError("가격·거래량 오류")


def align(near: list[Bar], far: list[Bar], now: datetime):
    def index(bars):
        out = {}
        for b in bars:
            validate_bar(b)
            if b.start in out:
                raise ValueError(f"중복 봉: {b.start}")
            if b.end <= now and at(b.start.date(), "09:00") <= b.start < at(b.start.date(), "15:20"):
                out[b.start] = b
        return out
    n, f = index(near), index(far)
    pairs = [PairBar(n[t], f[t]) for t in sorted(n.keys() & f.keys())]
    missing = {"near_only": [str(t) for t in sorted(n.keys() - f.keys())],
               "far_only": [str(t) for t in sorted(f.keys() - n.keys())]}
    return pairs, missing


def write_bars(path: Path, pairs: list[PairBar], *, provenance=None, responses=None):
    bars = [{"start": p.near.start.isoformat(), "near_open": p.near.open, "near_close": p.near.close,
             "near_volume": p.near.volume, "far_open": p.far.open, "far_close": p.far.close,
             "far_volume": p.far.volume} for p in pairs]
    digest = hashlib.sha256(json.dumps(bars, sort_keys=True, allow_nan=False).encode()).hexdigest()
    save_json(path, {"bars": bars, "bars_sha256": digest,
                    "provenance": provenance or {"kind": "unverified", "market_evidence": False},
                    "api_responses": responses or []})


def read_bars(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    bars = raw.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("가격 JSON에 bars 배열이 필요합니다")
    digest = hashlib.sha256(json.dumps(bars, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if raw.get("bars_sha256") and raw["bars_sha256"] != digest:
        raise ValueError("저장된 가격 자료의 해시가 다릅니다")
    pairs = []
    for row in bars:
        ts = datetime.fromisoformat(row["start"])
        n = Bar(ts, float(row["near_open"]), float(row["near_close"]), int(row["near_volume"]))
        far = Bar(ts, float(row["far_open"]), float(row["far_close"]), int(row["far_volume"]))
        validate_bar(n)
        validate_bar(far)
        if not at(ts.date(), "09:00") <= ts < at(ts.date(), "15:20"):
            raise ValueError("봉 시작시각은 09:00 이상 15:20 미만이어야 합니다")
        if pairs and ts <= pairs[-1].near.start:
            raise ValueError("가격 자료는 중복 없이 시간 오름차순이어야 합니다")
        pairs.append(PairBar(n, far))
    return pairs


def fetch_bars(api, symbol, day, journal):
    from backend.domestic_futureoption import domestic_futureoption_functions as nh
    bars = {}
    key = nh.FIRST_PAGE
    for _ in range(30):
        raw = nh.chart_minute(symbol, start_date=day.strftime("%Y%m%d"), end_date=day.strftime("%Y%m%d"),
                              start_time="090000", end_time="152000", minutes="5", next_key=key, api=api)
        journal.write("API_RESPONSE", api="domestic_chart-minute", symbol=symbol, response=raw)
        if raw.get("sym") and str(raw["sym"]).strip() != symbol:
            raise ValueError("요청 종목과 차트 응답 종목 불일치")
        rows = raw.get("Occurs1")
        if not isinstance(rows, list):
            raise ValueError("차트 Occurs1 누락")
        for r in rows:
            ymd = str(r.get("occurs1_ref_dt", "")).strip()
            if ymd != day.strftime("%Y%m%d"):
                continue
            tm = str(r.get("occurs1_exch_tm", "")).strip().zfill(6)
            b = Bar(datetime.strptime(ymd + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST),
                    float(r["occurs1_open_pric"]), float(r["occurs1_close_pric"]),
                    int(float(r["occurs1_cum_trd_qty"])))
            # 거래가 없는 봉은 0가격일 수 있습니다. 통계에서 제외되게 누락 처리합니다.
            if b.open > 0 and b.close > 0:
                validate_bar(b)
                bars.setdefault(b.start, b)
        nxt = str(raw.get("nxt_key") or "").strip()
        if not nxt or len(rows) < int(nh.CHART_ROWS):
            return sorted(bars.values(), key=lambda b: b.start)
        if nxt == key:
            raise RuntimeError("차트 연속조회 키 반복: 불완전 데이터 사용 중단")
        key = nxt
    raise RuntimeError("차트 연속조회 상한 초과")
