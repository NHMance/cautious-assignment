"""과거봉 재생: 신호 확정 뒤 다음 봉 시가에 양방향 슬리피지를 적용합니다."""
from __future__ import annotations
from dataclasses import asdict
import json
from .broker import SimBroker
from .broker import PairExecutor
from .journal import Journal
from .strategy import Quote, TICK, at
from .strategy import Strategy, exit_reason, entry_problem

def make_quotes(pair, cfg, opening=False):
    # 호가 원본이 없는 과거봉의 체결 모형입니다. 0틱 비용도 비교할 수 있습니다.
    ts = pair.near.start if opening else pair.ts
    n, f = (pair.near.open, pair.far.open) if opening else (pair.near.close, pair.far.close)
    slip = cfg.slippage_ticks * TICK
    return Quote("NEAR", ts, n - slip, n + slip), Quote("FAR", ts, f - slip, f + slip)


def run_replay(pairs, cfg, root, *, kind="replay", provenance=None):
    journal = Journal(root, kind, day=pairs[0].near.start.date())
    journal.path.unlink(missing_ok=True)  # week03처럼 같은 날짜의 리플레이는 새로 계산합니다.
    journal.write("RUN_START", settings=asdict(cfg), provenance=provenance,
                  execution_model="next_bar_open_with_adverse_slippage", account_orders=False)
    engine = PairExecutor(SimBroker(("NEAR", "FAR"), journal), "NEAR", "FAR", journal, cfg)
    strategy, pending = Strategy(cfg), None
    previous_day = None
    day_pnl = 0.0
    day_halted = False
    for pair in pairs:
        day = pair.near.start.date()
        if day != previous_day:
            if engine.position:
                journal.write("UNRESOLVED_POSITION", ts=pair.near.start, reason="MISSING_PRIOR_SESSION_CLOSE")
                break
            engine.entries = 0
            day_pnl = engine.realized
            day_halted = False
            pending = None
            previous_day = day
        nq, fq = make_quotes(pair, cfg, opening=True)
        daily_realized = engine.realized - day_pnl
        if pending:
            if pair.near.start == pending.ts and pair.near.start < at(day, cfg.entry_cutoff):
                problem = entry_problem(pending, nq, fq, cfg)
                if problem:
                    journal.write("SKIP", ts=pair.near.start, reason=problem)
                else:
                    engine.open(pending, nq, fq)
            else:
                journal.write("SKIP", ts=pair.near.start, reason="NEXT_BAR_MISSING_OR_CUTOFF")
            pending = None
        if engine.position:
            reason = exit_reason(engine.position, nq, fq, nq.ts, cfg, daily_realized)
            if reason:
                engine.close(nq, fq, reason)
                day_halted |= reason == "DAILY_LOSS"
        nq, fq = make_quotes(pair, cfg)
        if engine.position:
            reason = exit_reason(engine.position, nq, fq, pair.ts, cfg, engine.realized - day_pnl)
            if reason:
                engine.close(nq, fq, reason)
                day_halted |= reason == "DAILY_LOSS"
        signal, reason, stats = strategy.observe(pair)
        journal.write("OBSERVATION", ts=pair.ts, **stats, reason=reason,
                      near_close=pair.near.close, far_close=pair.far.close)
        if signal:
            block = ("HOLDING" if engine.position else "DAILY_LOSS" if day_halted or engine.realized - day_pnl <= -cfg.daily_loss_krw
                     else "MAX_ENTRIES" if engine.entries >= cfg.max_entries
                     else "ENTRY_CUTOFF" if pair.ts >= at(day, cfg.entry_cutoff) else None)
            if block:
                journal.write("SKIP", ts=pair.ts, reason=block)
            else:
                journal.write("SIGNAL", ts=pair.ts, signal=signal)
                pending = signal
    if pending:
        journal.write("SKIP", ts=pairs[-1].ts, reason="NO_NEXT_BAR")
    journal.write("RUN_END", ts=pairs[-1].ts, flat=engine.position is None,
                  positions=engine.broker.snapshot()[0], net_pnl=engine.realized,
                  unresolved=engine.position is not None)
    return summarize(journal.path)


def summarize(path):
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    trades = [r for r in records if r["type"] == "PAIR_CLOSED"]
    unwinds = [r for r in records if r["type"] == "UNWIND"]
    pnl = [r["net_pnl"] for r in records if r["type"] in ("PAIR_CLOSED", "UNWIND")]
    equity, peak, mdd = 0, 0, 0
    for v in pnl:
        equity += v
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    skip = {}
    for r in records:
        if r["type"] in ("SKIP", "OBSERVATION") and r.get("reason") not in (None, "SIGNAL"):
            k = r["reason"]
            skip[k] = skip.get(k, 0) + 1
    end = next((r for r in reversed(records) if r["type"] == "RUN_END"), {})
    closed = trades + unwinds
    summary = {"mode": records[0]["mode"], "provenance": records[0].get("provenance"),
               "entry_z": records[0]["settings"]["entry_z"], "closed_pairs": len(trades),
               "entry_attempts": sum(r["type"] == "ENTRY_ATTEMPT" for r in records),
               "unwinds": len(unwinds), "net_pnl": round(sum(pnl), 6),
               "gross_pnl": round(sum(r.get("gross_pnl", r["net_pnl"]) for r in closed), 6),
               "fees": sum(r.get("fees", 0) for r in closed),
               "win_rate": sum(v > 0 for v in pnl) / len(pnl) if pnl else None,
               "max_trade_loss": round(min([0] + pnl), 6), "closed_trade_drawdown": round(mdd, 6),
               "mean_hold_minutes": sum(r["hold_minutes"] for r in trades) / len(trades) if trades else None,
               "flat_confirmed": end.get("flat", False), "skip_reasons": skip,
               "limits": "5분봉 재생은 봉 내부 손절·실제 양 레그 체결 가능성·평가손익 최대낙폭을 검증하지 못함"}
    return summary

