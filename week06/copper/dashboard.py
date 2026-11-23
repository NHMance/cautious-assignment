"""Render live journal summaries and baseline/replay tables on localhost."""

import json
from collections import Counter
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .results import read_rows

MODES = {"paper": "NH 모의 운용", "function-test": "NH 기능 검증", "bot-test": "로컬 봇 테스트"}


def journal(folder):
    events = []
    for path in sorted(folder.rglob("journal_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return sorted(events, key=lambda event: event["ts"])


def table(columns, rows):
    labels = "".join(f"<th>{escape(label)}</th>" for _, label in columns)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(row.get(key, '')))}</td>" for key, _ in columns) + "</tr>"
                   for row in rows)
    if not body:
        return "<p class='empty'>기록 대기 중</p>"
    return f"<div class='scroll'><table><thead><tr>{labels}</tr></thead><tbody>{body}</tbody></table></div>"


def render(root, mode, day):
    mode = mode if mode in MODES else "paper"
    all_events = journal(root / mode)
    days = sorted({event["ts"][:10] for event in all_events}, reverse=True)
    day = day if day in days else (days[0] if days else "")
    events = [event for event in all_events if event["ts"].startswith(day)]
    trades = [event for event in events if event["type"] == "TRADE"]
    holds = Counter(event["reason"] for event in events if event["type"] == "HOLD")
    reasons = Counter(event["reason"] for event in trades)
    passed = [event for event in events if event["type"] == "BOT_TEST_PASS"]
    issues = [event for event in events if event["type"] in {"ERROR", "ORDER_REJECT", "LOCK_UNKNOWN"}]
    latest = {event["type"]: event for event in events if event["type"] in {"ACCOUNT_CHECK", "BAR", "WS_STATUS"}}
    account = latest.get("ACCOUNT_CHECK", {})
    ws = latest.get("WS_STATUS", {})
    bar = latest.get("BAR", {})
    if issues and issues[-1]["type"] in {"LOCK_UNKNOWN", "ORDER_REJECT"} and issues[-1]["ts"] >= account.get("ts", ""):
        account_text = "수동 대사 필요"
    else:
        account_text = f"잔고 {account['position_qty']} · 미체결 {account['pending']}" if account else "확인 대기"
    confirmed_pnl = f"${sum(event['net'] for event in trades):,.2f}" if events else "—"
    cards = "".join(f"<div class='card'><small>{label}</small><strong>{escape(str(value))}</strong></div>"
                    for label, value in (("실현손익", confirmed_pnl), ("완료 거래", len(trades)),
                                         ("진입 보류", sum(holds.values())), ("REST 오류", len(issues)),
                                         ("최종 계좌 확인", account_text), ("최근 국면", bar.get("regime") or "—"),
                                         ("실시간 연결", ws.get("event") or "—")))
    tabs = "".join(f'<a class="{"active" if key == mode else ""}" href="/?mode={key}">{label}</a>'
                   for key, label in MODES.items())
    options = "".join(f'<option {"selected" if value == day else ""}>{escape(value)}</option>' for value in days)
    counts = table((("reason", "사유 코드"), ("count", "횟수")),
                   ({"reason": key, "count": count} for key, count in (holds + reasons).most_common()))
    trade_table = table((("exit_at", "청산 시각"), ("regime", "국면"), ("side", "방향 ±1"),
                         ("entry_price", "진입가"), ("exit_price", "청산가"), ("reason", "청산 사유"),
                         ("net", "순손익 USD")), reversed(trades))
    errors = table((("ts", "시각 KST"), ("type", "구분"), ("api", "API"), ("code", "코드"),
                    ("message", "오류 원문"), ("action", "대응")), reversed(issues[-100:]))
    tests = table((("rule", "규칙"), ("reason", "사유 코드"), ("orders", "주문"), ("position_qty", "잔고"),
                   ("pending", "미체결")), passed)
    coverage = table((("hour", "KST"), ("expected", "기대 봉"), ("received", "수신 봉"), ("missing", "누락률")),
                     (dict(row, missing=f"{float(row['missing_ratio']):.1%}")
                      for row in read_rows(root / "raw" / "coverage.csv")))
    replay = table((("strategy", "전략"), ("trades", "거래"), ("holds", "보류"), ("net", "순손익 USD"),
                    ("mdd", "낙폭 USD")), (row for row in read_rows(root / "replay" / "summary.csv")
                                            if row["regime"] == "ALL"))
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10"><title>6주차 · HGH27 위험관리</title>
<style>
*{{box-sizing:border-box}} body{{font:15px system-ui,sans-serif;background:#f2f5f8;color:#182b3e;margin:0}}
main{{max-width:1250px;margin:auto;padding:32px 20px}} header{{margin-bottom:24px}}
h1{{margin:0 0 8px;font-size:clamp(25px,4vw,38px)}} h2{{font-size:18px;margin:0 0 14px}}
p,small{{color:#476278}} nav{{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}}
nav a{{color:#1b4965;padding:10px 13px;background:#fff;border-radius:7px;text-decoration:none}}
nav a.active{{background:#14445b;color:#fff}} select{{padding:6px;border:1px solid #98aabd;border-radius:4px}}
.cards,.grid{{display:grid;gap:12px}} .cards{{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin:16px 0}}
.grid{{grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr));margin:12px 0}}
.card,section{{background:#fff;border:1px solid #dce5ec;border-radius:10px;padding:16px}}
.card strong{{display:block;margin-top:5px;font-size:18px}} .scroll{{overflow:auto;max-height:360px}}
table{{border-collapse:collapse;width:100%;text-align:left;font-variant-numeric:tabular-nums}}
th,td{{border-bottom:1px solid #e3eaf0;padding:8px 10px;white-space:nowrap}} th{{position:sticky;top:0;background:#eff5f8}}
.empty{{margin:0}} section.wide{{margin:12px 0}}
</style></head><body><main>
<header><h1>HGH27 · 6주차 위험관리</h1>
<p>전략 C · 17:00–23:30 KST · 22:20 진입 제한 · 22:45 강제청산</p></header>
<nav aria-label="운영 유형">{tabs}</nav>
<form><input type="hidden" name="mode" value="{mode}">
<label>거래일 KST <select name="day" onchange="this.form.submit()">{options or '<option>기록 대기</option>'}</select></label>
</form><div class="cards">{cards}</div>
<div class="grid"><section><h2>사유별 집계</h2>{counts}</section><section><h2>로컬 시나리오 통과 기록</h2>{tests}</section></div>
<section class="wide"><h2>체결된 거래</h2>{trade_table}</section>
<section class="wide"><h2>REST 오류 리포트</h2>{errors}</section>
<div class="grid"><section><h2>기준 기간 시간대별 봉 품질</h2>{coverage}</section>
<section><h2>A/B/C 리플레이</h2>{replay}</section></div>
</main></body></html>"""


def serve(root, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlsplit(self.path)
            if request.path != "/":
                self.send_error(404)
                return
            params = parse_qs(request.query)
            page = render(root, params.get("mode", ["paper"])[0], params.get("day", [""])[0])
            payload = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return
