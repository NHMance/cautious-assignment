"""--replay-date 의 가상 시세: 미래 봉을 보지 않는지 (API 호출 없음)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from bot import backtest
from bot.backtest import HistoricalMarket
from bot.clock import KST, SimClock, at
from bot.strategy import Bar

DAY = date(2026, 10, 28)


def bars(minutes: int, closes: list[float]) -> list[Bar]:
    start = at(DAY, "09:00")
    return [Bar(start + timedelta(minutes=minutes * i), start + timedelta(minutes=minutes * (i + 1)), c, c, c, c)
            for i, c in enumerate(closes)]


@pytest.fixture
def market(tmp_path, monkeypatch):
    data = {5: bars(5, [100, 101, 102]), 1: bars(1, [100, 100.5, 101, 101.5])}
    monkeypatch.setattr(backtest.Market, "fetch_bars", lambda self, day, minutes: data[minutes])
    clock = SimClock(datetime(2026, 10, 28, 8, 55, tzinfo=KST))
    return HistoricalMarket("TEST", DAY, clock, root=tmp_path), clock


def test_only_confirmed_bars_are_visible(market):
    m, clock = market
    clock.sleep(15 * 60)  # 09:10
    assert [b.close for b in m.get_bars_5m(DAY, clock.now())] == [100, 101]
    assert (m.path.parent / "bars_TEST_20261028.json").is_file()


NOW = datetime(2026, 10, 29, 1, 30, tzinfo=KST)  # 목요일 새벽


@pytest.mark.parametrize(("day", "now", "ok"), [
    (date(2026, 10, 28), NOW, True),                                     # 어제(수)
    (date(2026, 10, 23), NOW, True),                                     # 지난주 금요일
    (date(2026, 10, 25), NOW, False),                                    # 일요일
    (date(2026, 10, 24), NOW, False),                                    # 토요일
    (date(2026, 10, 30), NOW, False),                                    # 내일
    (date(2026, 10, 29), NOW, False),                                    # 오늘, 장 시작 전
    (date(2026, 10, 29), datetime(2026, 10, 29, 14, 0, tzinfo=KST), False),  # 오늘, 장중
    (date(2026, 10, 29), datetime(2026, 10, 29, 15, 40, tzinfo=KST), True),  # 오늘, 장 마감 뒤
])
def test_replay_day_problem(day, now, ok):
    assert (backtest.replay_day_problem(day, now) is None) is ok


def test_empty_day_does_not_overwrite_saved_bars(tmp_path, monkeypatch):
    saved = tmp_path / "bars_TEST_20261028.json"
    saved.write_text("이전에 받아 둔 봉", encoding="utf-8")
    monkeypatch.setattr(backtest.Market, "fetch_bars", lambda self, day, minutes: [])
    clock = SimClock(datetime(2026, 10, 28, 8, 55, tzinfo=KST))
    m = HistoricalMarket("TEST", DAY, clock, root=tmp_path)
    assert m.bars_5m == []
    assert saved.read_text(encoding="utf-8") == "이전에 받아 둔 봉"


def test_price_is_last_confirmed_1m_close(market):
    m, clock = market
    with pytest.raises(ValueError):
        m.get_price()  # 08:55, 확정된 1분봉 없음
    clock.sleep(7 * 60 + 30)  # 09:02:30
    assert m.get_price() == 100.5
