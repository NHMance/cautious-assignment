from dataclasses import replace
from datetime import datetime, timedelta
import json
import pytest

from spread.strategy import Settings, Bar, PairBar, KST
from spread.market import align, read_bars, write_bars
from spread.replay import run_replay
from spread.journal import clean


def records(path):
    return [json.loads(line) for line in next(path.glob("journal_*.jsonl")).read_text().splitlines()]


def test_align_matches_time_and_excludes_unconfirmed():
    pairs = synthetic_pairs()[:4]
    n, f = [p.near for p in pairs], [p.far for p in pairs]
    result, missing = align(n, f[:2] + f[3:], pairs[2].ts)
    assert len(result) == 2
    assert missing["near_only"] == [str(n[2].start)]


def test_duplicate_timestamps_fail_instead_of_silent_replacement():
    p = synthetic_pairs()[0]
    with pytest.raises(ValueError):
        align([p.near, p.near], [p.far], p.ts)


def test_price_json_roundtrip_and_duplicate_rejection(tmp_path):
    path = tmp_path / "bars.json"
    pairs = synthetic_pairs()
    write_bars(path, pairs)
    assert read_bars(path) == pairs
    raw = json.loads(path.read_text())
    raw["bars"].append(raw["bars"][-1])
    raw.pop("bars_sha256")
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        read_bars(path)


def test_replay_fills_at_next_open_not_signal_close(tmp_path):
    result = run_replay(synthetic_pairs(), Settings(), tmp_path, kind="test")
    log = records(tmp_path)
    signal = next(r for r in log if r["type"] == "SIGNAL")
    fill = next(r for r in log if r["type"] == "FILL")
    assert fill["ts"] == signal["ts"]  # 다음 봉 시작시각은 직전 봉 확정시각과 같습니다.
    assert fill["fill"]["price"] == pytest.approx(1445.1)
    assert result["flat_confirmed"]
    assert result["closed_pairs"] >= 1


def test_cost_sensitivity_changes_same_trades_pnl(tmp_path):
    cfg = Settings()
    zero = run_replay(synthetic_pairs(), replace(cfg, slippage_ticks=0), tmp_path / "zero")
    paid = run_replay(synthetic_pairs(), replace(cfg, fee_per_leg_side=500), tmp_path / "paid")
    assert paid["net_pnl"] < zero["net_pnl"]
    assert paid["fees"] == paid["closed_pairs"] * 4 * 500


def test_entry_cap_and_cutoff(tmp_path):
    cfg = Settings(entry_z=1.0, max_entries=1)
    run_replay(synthetic_pairs(), cfg, tmp_path)
    log = records(tmp_path)
    assert sum(r["type"] == "ENTRY_ATTEMPT" for r in log) == 1
    assert any(r["type"] == "SKIP" and r["reason"] == "MAX_ENTRIES" for r in log)
    assert all(r["ts"][11:16] < "15:00" for r in log if r["type"] == "ENTRY_ATTEMPT")


def test_replay_early_end_does_not_invent_liquidation(tmp_path):
    pairs = synthetic_pairs()[:26]
    result = run_replay(pairs, Settings(), tmp_path)
    assert result["flat_confirmed"] is False
    assert result["closed_pairs"] == 0


def test_no_next_bar_does_not_fill_signal(tmp_path):
    run_replay(synthetic_pairs()[:25], Settings(), tmp_path)
    log = records(tmp_path)
    assert not any(r["type"] == "FILL" for r in log)
    assert any(r.get("reason") == "NO_NEXT_BAR" for r in log)


def test_missing_next_bar_does_not_fill_delayed_signal(tmp_path):
    pairs = synthetic_pairs()
    run_replay(pairs[:25] + pairs[26:], Settings(), tmp_path)
    log = records(tmp_path)
    assert any(r.get("reason") == "NEXT_BAR_MISSING_OR_CUTOFF" for r in log)


def test_nested_privacy_fields_are_masked():
    result = clean({"response": [{"ac_no": "SENSITIVE", "access_token": "SENSITIVE", "last_pric": "1445"}]})
    assert "SENSITIVE" not in str(result)
    assert result["response"][0]["last_pric"] == "1445"


def test_compare_cli_keeps_only_dated_journals_and_reuses_paths(tmp_path, monkeypatch, capsys):
    import main
    monkeypatch.setattr(main.Settings, "load", lambda: Settings())
    out = tmp_path / "compare"
    for _ in range(2):
        assert main.main(["compare", "--output", str(out)]) == 0
        assert {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} == {
            "A/journal_20261109.jsonl", "B/journal_20261109.jsonl", "C/journal_20261109.jsonl"}
        assert [main.summarize(out / label / "journal_20261109.jsonl")["net_pnl"] for label in "ABC"] == [-5000, 0, 0]
        assert [main.summarize(out / label / "journal_20261109.jsonl")["entry_attempts"] for label in "ABC"] == [2, 0, 0]
    assert "-5,000원" in capsys.readouterr().out


def test_default_cli_only_checks_and_never_runs_orders(tmp_path, monkeypatch):
    import main
    from types import SimpleNamespace
    from spread.strategy import Quote, KST
    calls = []
    fake_client = SimpleNamespace(session=SimpleNamespace(close=lambda: calls.append("closed")))
    monkeypatch.setattr(main, "create_client", lambda env_file: fake_client)
    monkeypatch.setattr(main, "verify_prices", lambda *args: None)
    def quote(client, symbol, journal):
        return Quote(symbol, datetime.now(KST), 1445, 1445.1)
    monkeypatch.setattr(main, "quote", quote)
    monkeypatch.setattr(main, "PaperBroker", lambda *args: SimpleNamespace(snapshot=lambda: ({"A756B000": 0, "A756C000": 0}, [])))
    def no_orders(*args, **kwargs):
        pytest.fail("기본 명령에서 모의 주문 루프를 실행하면 안 됩니다")
    monkeypatch.setattr(main, "run_paper", no_orders)
    out = tmp_path / "check"
    assert main.main(["--output", str(out)]) == 0
    assert records(out)[-1]["orders_submitted"] is False
    assert records(out)[-1]["type"] == "CHECK"
    assert len(list(out.iterdir())) == 1
    assert calls == ["closed"]


def test_collect_cli_saves_prices_and_api_responses_in_one_file(tmp_path, monkeypatch):
    import main
    from types import SimpleNamespace
    pairs = synthetic_pairs()
    calls = []
    client = SimpleNamespace(session=SimpleNamespace(close=lambda: calls.append("closed")))
    monkeypatch.setattr(main, "create_client", lambda env_file: client)
    def fetch(client, symbol, day, journal):
        journal.write("API_RESPONSE", api="test", response={"symbol": symbol})
        return [p.near if symbol == "A756B000" else p.far for p in pairs]
    monkeypatch.setattr(main, "fetch_bars", fetch)
    out = tmp_path / "collect"
    assert main.main(["collect", "--date", "20261109", "--output", str(out)]) == 0
    assert [p.name for p in out.iterdir()] == ["bars_USD_20261109.json"]
    assert read_bars(out / "bars_USD_20261109.json") == pairs
    assert len(json.loads((out / "bars_USD_20261109.json").read_text())["api_responses"]) == 2
    assert calls == ["closed"]


def synthetic_pairs():
    """시장 자료가 아닌 결정적 기능 점검 자료입니다."""
    start = datetime(2026, 11, 9, 9, tzinfo=KST)
    spreads = [0.6, 1.4] * 12 + [1.8, 1.8, 1.0, 0.6, 1.4, 0.2, 0.2, 1.0]
    spreads += [0.6, 1.4] * 22
    pairs = []
    for i, s in enumerate(spreads):
        ts = start + timedelta(minutes=5 * i)
        if ts.hour > 15 or (ts.hour == 15 and ts.minute >= 20):
            break
        previous = spreads[i - 1] if i else s
        pairs.append(PairBar(Bar(ts, 1445.0, 1445.0, 20),
                             Bar(ts, round(1445.0 + previous, 1), round(1445.0 + s, 1), 10)))
    return pairs
