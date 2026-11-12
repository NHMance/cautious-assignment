import json

import pytest

from frontend.app import aggregate, export, load_results, render_html, RESULTS


def test_aggregate_uses_chronological_closed_equity_across_days():
    sessions = [
        {"summary": {"flat_confirmed": True}, "trades": [
            {"exit_ts": "2026-11-10T10:00", "net_pnl": -8},
            {"exit_ts": "2026-11-10T11:00", "net_pnl": 4}]},
        {"summary": {"flat_confirmed": True}, "trades": [
            {"exit_ts": "2026-11-09T10:00", "net_pnl": 10}]},
    ]
    assert aggregate(sessions) == {"pnl": 6, "trades": 3, "wins": 2, "drawdown": 8, "flat": True}
    assert aggregate([])["flat"] is False


def test_saved_replay_dashboard_reconciles_all_scenarios():
    data = load_results()
    assert len(data["inputs"]) == 3
    assert sum(s["bars"] for s in data["inputs"]) == 228
    assert len(data["sessions"]) == 27
    expected = {"base": [(-16000, 8), (-3000, 2), (0, 0)],
                "zero": [(16000, 8), (5000, 2), (0, 0)],
                "high": [(-80000, 8), (-20000, 2), (0, 0)]}
    for scenario, values in expected.items():
        for variant, (pnl, trades) in zip("ABC", values):
            sessions = [s for s in data["sessions"] if s["scenario"] == scenario and s["variant"] == variant]
            result = aggregate(sessions)
            assert (result["pnl"], result["trades"], result["flat"]) == (pnl, trades, True)
            for s in sessions:
                assert sum(t["net_pnl"] for t in s["trades"]) == s["summary"]["net_pnl"]


def test_dashboard_rejects_tampered_price_file(tmp_path):
    raw = json.loads((RESULTS / "bars_USD_20261109.json").read_text())
    raw["bars"][0]["near_close"] += 1
    (tmp_path / "bars_USD_20261109.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="해시"):
        load_results(tmp_path)


def test_export_does_not_touch_journals_and_works_without_api(tmp_path, monkeypatch):
    import shutil
    from spread import market
    def no_client(*args, **kwargs):
        pytest.fail("대시보드는 API 인증·접속을 하면 안 됩니다")
    monkeypatch.setattr(market, "create_client", no_client)
    source = RESULTS / "A/journal_20261109.jsonl"
    (tmp_path / "A").mkdir()
    target = tmp_path / "A" / source.name
    shutil.copyfile(source, target)
    before = target.read_bytes()
    data = export(tmp_path)
    assert target.read_bytes() == before
    assert len(data["sessions"]) == 1
    html = (tmp_path / "dashboard.html").read_text()
    assert "__REPLAY_DATA__" not in html
    assert "-5,000원 / 2회" in (tmp_path / "README.md").read_text()
    assert "api_responses" not in html


def test_embedded_json_cannot_terminate_script():
    html = render_html({"value": "</script><script>alert('x')</script>&"})
    assert "</script><script>alert" not in html
    assert "\\u003c/script>" in html
