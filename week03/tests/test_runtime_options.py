"""실제 접속 없이 실행 옵션과 3시간 합성 실행을 확인합니다."""
import json

import pytest

from trader import settings
from trader import journal as journal_mod
from trader.main import main


def test_requested_profile_runs_without_api(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "RESULTS_DIR", tmp_path)
    original = settings.BAR_MINUTES, settings.ENFORCE_ENTRY_LIMITS
    assert main(["--strategy", "A", "--bar-minutes", "1", "--no-entry-limits",
                 "--duration-minutes", "180", "--bot-test"]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "bot-test/A/journal_20261102.jsonl").read_text().splitlines()]
    start = rows[0]
    assert start["settings"]["BAR_MINUTES"] == 1
    assert start["settings"]["ENFORCE_ENTRY_LIMITS"] is False
    assert start["settings"]["EMA_FAST"] == 9 and start["settings"]["EMA_SLOW"] == 21
    assert start["duration_minutes"] == 180
    assert start["session"]["entry_cutoff"] == start["session"]["close"]
    assert start["session"]["force_close"] == start["session"]["close"]
    assert not any(r["type"] == "ATR_FILTER_BLOCK" for r in rows)
    assert rows[-1]["type"] == "RUN_END" and rows[-1]["position"] is None
    assert (settings.BAR_MINUTES, settings.ENFORCE_ENTRY_LIMITS) == original


@pytest.mark.parametrize("options", [
    ["--strategy", "A", "--duration-minutes", "0"],
    ["--strategy", "B", "--duration-minutes", "180"],
    ["--strategy", "A", "--duration-minutes", "180", "--session", "10:00-13:00"],
    ["--strategy", "A", "--duration-minutes", "180", "--replay-date", "20261102"],
])
def test_invalid_duration_options_are_rejected_before_auth(options):
    with pytest.raises(SystemExit) as exc:
        main(options)
    assert exc.value.code == 2


def test_fixed_afternoon_session_and_liquidation_without_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "RESULTS_DIR", tmp_path)
    assert main(["--strategy", "A", "--bar-minutes", "1", "--no-entry-limits",
                 "--session", "15:00-18:00", "--force-close-at", "17:50", "--bot-test"]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "bot-test/A/journal_20261102.jsonl").read_text().splitlines()]
    session = rows[0]["session"]
    assert session["open"] == "2026-11-02T15:00:00+09:00"
    assert session["close"] == "2026-11-02T18:00:00+09:00"
    assert session["entry_cutoff"] == session["force_close"] == "2026-11-02T17:50:00+09:00"
    assert rows[0]["duration_minutes"] is None
    assert rows[-1]["ts"] == "2026-11-02T18:00:00+09:00"
    assert any(row["type"] == "WAIT_OPEN" for row in rows)
    for row in rows:
        if row["type"] == "ENTRY_OPEN":
            assert session["open"] <= row["ts"] < session["entry_cutoff"]


@pytest.mark.parametrize("options", [
    ["--force-close-at", "17:50"],
    ["--session", "15:00-18:00", "--force-close-at", "18:01"],
    ["--session", "15:00-18:00", "--force-close-at", "14:59"],
    ["--session", "15:00-18:00", "--force-close-at", "25:50"],
])
def test_invalid_fixed_liquidation_options_before_auth(options):
    with pytest.raises(SystemExit) as exc:
        main(["--strategy", "A", "--bot-test", *options])
    assert exc.value.code == 2


def test_reverse_profile_records_original_instrument_and_flat_end(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "RESULTS_DIR", tmp_path)
    assert main(["--strategy", "A", "--symbol", "MKUX26", "--bar-minutes", "1", "--no-entry-limits",
                 "--reverse-on-cross", "--session", "15:00-18:00", "--force-close-at", "17:50",
                 "--bot-test"]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "bot-test/A/journal_20261102.jsonl").read_text().splitlines()]
    assert rows[0]["reverse_on_cross"] is True
    assert rows[0]["settings"]["SYMBOL"] == "MKUX26"
    assert rows[-1]["type"] == "RUN_END" and rows[-1]["position"] is None


def test_reverse_option_rejects_b():
    with pytest.raises(SystemExit) as exc:
        main(["--strategy", "B", "--reverse-on-cross", "--bot-test"])
    assert exc.value.code == 2


def test_trend_profile_implies_reversal_and_exits_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "RESULTS_DIR", tmp_path)
    assert main(["--strategy", "A", "--bar-minutes", "1", "--no-entry-limits", "--trend-entry",
                 "--session", "15:00-18:00", "--force-close-at", "17:50", "--bot-test"]) == 0
    rows = [json.loads(line) for line in
            (tmp_path / "bot-test/A/journal_20261102.jsonl").read_text().splitlines()]
    assert rows[0]["trend_entry"] is True and rows[0]["reverse_on_cross"] is True
    assert any(r["type"] == "ENTRY_SIGNAL" and r.get("entry_mode") == "EMA_TREND" for r in rows)
    assert sum(r["type"] == "ENTRY_SIGNAL" and r.get("entry_mode") == "EMA_TREND" for r in rows) > 1
    assert rows[-1]["position"] is None
    assert all(r["ts"] < rows[0]["session"]["entry_cutoff"] for r in rows if r["type"] == "ENTRY_OPEN")


def test_trend_option_rejects_b():
    with pytest.raises(SystemExit) as exc:
        main(["--strategy", "B", "--trend-entry", "--bot-test"])
    assert exc.value.code == 2
