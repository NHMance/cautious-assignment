from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from copper.bot_test import EXPECTED, RestAccount, scenario
from copper.live import Feed, OrderRejected, PaperExecution, Quote, RestJournal, RiskSettings, TradingLocked
from copper.market import api
from copper.strategy import KST
from bot.clock import SimClock


@pytest.mark.parametrize("rule", EXPECTED)
def test_risk_rule(tmp_path, rule):
    assert scenario(tmp_path, rule) == EXPECTED[rule]


@pytest.mark.parametrize("timeout", [True, False])
def test_rest_error_is_reported_and_blocks_reorder(tmp_path, monkeypatch, timeout):
    at = datetime(2026, 11, 23, 17, tzinfo=KST)
    journal = RestJournal("HGH27", root=tmp_path, clock=SimClock(at), echo=False)
    execution = PaperExecution("HGH27", journal)
    execution.broker = RestAccount()
    order = Mock(side_effect=TimeoutError("NH response timeout")) if timeout else Mock(
        return_value={"err_cd": "ABC", "err_msg": "order rejected", "acnt_no": "private"})
    monkeypatch.setattr(api, "order", order)
    with pytest.raises(TradingLocked if timeout else OrderRejected):
        execution.execute(1, 5, entry=True, clock=lambda: at)
    with pytest.raises(TradingLocked):
        execution.execute(1, 5, entry=True, clock=lambda: at)
    assert order.call_count == 1
    report = (tmp_path / "api-errors.jsonl").read_text()
    assert ("LOCK_UNKNOWN" if timeout else "ORDER_REJECT") in report
    assert "private" not in report


@pytest.mark.parametrize("hour,minute", [(16, 59), (22, 20), (22, 45), (23, 30)])
def test_entry_outside_window(tmp_path, monkeypatch, hour, minute):
    at = datetime(2026, 11, 23, hour, minute, tzinfo=KST)
    execution = PaperExecution("HGH27", RestJournal("HGH27", root=tmp_path, clock=SimClock(at), echo=False))
    execution.broker = RestAccount()
    send = Mock()
    monkeypatch.setattr(api, "order", send)
    assert execution.execute(1, 5, entry=True, clock=lambda: at) is None
    send.assert_not_called()


def test_websocket_quote_expires_at_ten_seconds():
    at = datetime(2026, 11, 23, 17, tzinfo=KST)
    source = Mock(quotes={"HGH27": Quote(at, 5, 5.001)})
    assert Feed.quote(source, "HGH27", at + timedelta(seconds=9))
    assert Feed.quote(source, "HGH27", at + timedelta(seconds=10)) is None
