"""추가 의존성 없이 실행: python -m frontend.app [--export]."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from spread.market import read_bars
from spread.replay import summarize
from spread.strategy import KST, ROOT
from spread.journal import clean

TEMPLATE = Path(__file__).with_name("index.html")
RESULTS = ROOT / "results/replay"
SCENARIOS = (
    ("base", "기본 · 1틱 / 0원", Path(".")),
    ("zero", "비용 제외 · 0틱 / 0원", Path("비용비교/0틱")),
    ("high", "비용 확대 · 2틱 / 500원", Path("비용비교/2틱_수수료500")),
)


def load_results(root: Path = RESULTS):
    """인증·주문 없이 고정된 리플레이 경로에서 필요한 필드만 읽습니다."""
    sessions = []
    inputs = []
    for path in sorted(root.glob("bars_USD_*.json")):
        pairs = read_bars(path)  # 원본 해시·시각·가격 검증
        raw = json.loads(path.read_text(encoding="utf-8"))
        inputs.append({"day": path.stem[-8:], "bars": len(pairs),
                       "sha256": raw.get("bars_sha256"),
                       "source": raw.get("provenance", {}).get("kind"),
                       "missing": raw.get("provenance", {}).get("missing", {})})
    for key, label, folder in SCENARIOS:
        for variant in "ABC":
            for path in sorted((root / folder / variant).glob("journal_*.jsonl")):
                records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if not records or records[0].get("mode") != "replay":
                    continue
                start = records[0]
                observations = [{k: r.get(k) for k in
                                 ("ts", "spread", "mean", "std", "z", "reason", "near_close", "far_close")}
                                for r in records if r["type"] == "OBSERVATION"]
                trades = []
                for r in records:
                    if r["type"] not in ("PAIR_CLOSED", "UNWIND"):
                        continue
                    entry = r.get("entry", {})
                    trades.append({"exit_ts": r["ts"], "entry_ts": entry.get("near", {}).get("ts"),
                                   "direction": entry.get("signal", {}).get("direction"),
                                   "reason": r.get("reason", r["type"]),
                                   "net_pnl": r["net_pnl"], "gross_pnl": r.get("gross_pnl"),
                                   "fees": r.get("fees", 0), "hold_minutes": r.get("hold_minutes"),
                                   "entry_near": entry.get("near", {}).get("price"),
                                   "entry_far": entry.get("far", {}).get("price"),
                                   "exit_near": r.get("exit_near", {}).get("price"),
                                   "exit_far": r.get("exit_far", {}).get("price")})
                sessions.append({"scenario": key, "label": label, "variant": variant,
                                 "day": path.stem[-8:], "settings": start["settings"],
                                 "summary": summarize(path), "observations": observations,
                                 "trades": trades,
                                 "logs": [clean({k: r[k] for k in
                                          ("ts", "type", "mode", "reason", "settings", "execution_model",
                                           "signal", "position", "fill", "role", "side", "symbol", "order_no",
                                           "net_pnl", "gross_pnl", "fees", "realized", "hold_minutes",
                                           "spread", "mean", "std", "z", "near_close", "far_close", "flat")
                                          if k in r}) for r in records if r["type"] != "API_RESPONSE"],
                                 "signals": sum(r["type"] == "SIGNAL" for r in records),
                                 "skips": dict(Counter(r["reason"] for r in records if r["type"] == "SKIP")),
                                 "exit_reasons": dict(Counter(t["reason"] for t in trades))})
    return {"generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "inputs": inputs, "sessions": sessions,
            "scenarios": [{"id": k, "label": label} for k, label, _ in SCENARIOS]}


def aggregate(sessions):
    trades = sorted((t for s in sessions for t in s["trades"]), key=lambda t: t["exit_ts"])
    equity = peak = drawdown = 0
    for trade in trades:
        equity += trade["net_pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"pnl": round(equity, 6), "trades": len(trades),
            "wins": sum(t["net_pnl"] > 0 for t in trades), "drawdown": round(drawdown, 6),
            "flat": bool(sessions) and all(s["summary"]["flat_confirmed"] for s in sessions)}


def render_html(data):
    # HTML을 종료할 수 있는 문자까지 이스케이프해 독립 HTML에서도 안전하게 읽습니다.
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    return TEMPLATE.read_text(encoding="utf-8").replace("__REPLAY_DATA__", payload)


def report(data):
    days = sorted({s["day"] for s in data["sessions"]})
    lines = ["# 미국달러선물 스프레드 과거 데이터 시뮬레이션", "",
             f"기간: {', '.join(days)} (KST) · 두 월물 공통 5분봉 {sum(i['bars'] for i in data['inputs'])}개", "",
             "[대시보드 열기](dashboard.html) · 원본: `bars_USD_날짜.json` · 전략별 근거: `{A,B,C}/journal_날짜.jsonl`", "",
             "NH 모의 API에서 조회한 실제 과거 가격을 재생한 결과입니다. 계좌 주문·체결 실적이 아닙니다.", "",
             "A/B/C는 진입 기준 1.0σ / 1.5σ / 2.0σ이며 다른 설정은 같습니다. 직전 24개 봉 통계로 신호를 판단하고 다음 봉 시가에 비용 가정을 적용합니다.", "",
             "## 진입 기준 비교 (편도 1틱·레그당 편도 수수료 0원 가정)", "",
             "| 거래일 | A 손익 / 거래 | B 손익 / 거래 | C 손익 / 거래 |", "| --- | ---: | ---: | ---: |"]
    for day in days + ["합계"]:
        row = [day]
        for v in "ABC":
            a = aggregate([s for s in data["sessions"] if s["scenario"] == "base" and s["variant"] == v and (day == "합계" or s["day"] == day)])
            row.append(f"{a['pnl']:,.0f}원 / {a['trades']}회")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## 비용 가정 비교", "", "수수료는 레그당 편도 금액입니다. 왕복 한 쌍은 총 4회 주문이므로 500원 가정에서는 2,000원입니다. 슬리피지도 4회에 각각 적용합니다.", "",
              "| 비용 가정 | A 손익 / 거래 | B 손익 / 거래 | C 손익 / 거래 |", "| --- | ---: | ---: | ---: |"]
    for sc in data["scenarios"]:
        row = [sc["label"]]
        for v in "ABC":
            a = aggregate([s for s in data["sessions"] if s["scenario"] == sc["id"] and s["variant"] == v])
            row.append(f"{a['pnl']:,.0f}원 / {a['trades']}회")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## 함께 비교할 점", "",
              "- A는 B보다 거래 기회가 많았습니다. 기본 비용 가정에서 두 기준 모두 손실이었으며, C는 거래가 없어 수익성 판단 근거가 없습니다.",
              "- 비용 제외 결과와 비용 반영 결과를 함께 보셔야 합니다. 비용 확대 시 금액 손절이 작동할 수 있어 같은 손익에서 비용 차이만 빼는 비교는 성립하지 않습니다.",
              "- 대시보드는 거래 횟수·승률·청산 거래 기준 누적손익 최대낙폭·평균 보유시간·청산 사유와 진입하지 않은 사유를 함께 표시합니다. 거래가 없으면 승률은 계산하지 않습니다.",
              f"- {len(days)}거래일 표본이며 실시간 KB 호가 전략과 관측 주기가 다릅니다. 5분봉 내부 가격 경로, 평가손익 최대낙폭, 실제 양 레그 체결·대기시간은 검증하지 못합니다.",
              "- 비용은 실측 수수료·슬리피지가 아닌 가정입니다. 이 결과만으로 진입 기준을 확정하지 않으며 기간·유동성·비용 민감도를 함께 비교할 수 있습니다.", "",
              "## 재현 및 대시보드", "", "`week04/`에서 실행합니다.", "", "```bash",
              "# 저장된 완료 거래일 전체 × A/B/C × 비용 3가정 재생 후 대시보드·이 문서 갱신",
              ".venv/bin/python -m frontend.simulate", "",
              "# 로컬 대시보드 (저장 파일만 읽음)", ".venv/bin/python -m frontend.app --port 8004",
              "# http://127.0.0.1:8004", "```", "",
              "`dashboard.html`은 브라우저에서 직접 열 수 있는 독립 파일입니다. 새 기록 반영은 위 명령으로 다시 생성합니다.", "",
              "## 입력 자료 확인", "", "| 날짜 | 공통 봉 | 가격 SHA-256 |", "| --- | ---: | --- |"]
    for source in data["inputs"]:
        lines.append(f"| {source['day']} | {source['bars']} | `{source['sha256']}` |")
    lines += ["", f"생성 시각: {data['generated_at']}", ""]
    return "\n".join(lines)


def export(root: Path = RESULTS):
    data = load_results(root)
    if not data["sessions"]:
        raise ValueError("리플레이 기록이 없습니다. 먼저 compare를 실행하세요.")
    (root / "dashboard.html").write_text(render_html(data), encoding="utf-8")
    (root / "README.md").write_text(report(data), encoding="utf-8")
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/api/results"):
            self.send_error(404)
            return
        try:
            data = load_results()
            content = render_html(data) if path == "/" else json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (OSError, ValueError, KeyError):
            self.send_error(500, "Cannot read replay files")
            return
        payload = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if path == "/" else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true", help="독립 HTML과 결과 문서 갱신")
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()
    if args.export:
        export()
        print(f"생성: {RESULTS / 'dashboard.html'}")
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"리플레이 대시보드: http://127.0.0.1:{args.port} (Ctrl+C로 종료)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
