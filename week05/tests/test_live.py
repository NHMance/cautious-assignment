from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from copper.live import Feed, PaperExecution
from copper.strategy import KST


def test_websocket_book_expires_and_disconnect_clears_data():
    feed = Feed({"MHGH27": "MHGH27"}, None, Mock(), Mock())
    feed.data({"header": {"action_code": "FB", "action_sym": "MHGH27"},
               "body": {"bid_pric_1": "4.0000", "ask_pric_1": "4.0015"}})
    at = feed.quotes["MHGH27"].at
    assert feed.quote("MHGH27", at + timedelta(seconds=10)).spread == pytest.approx(3)
    assert feed.quote("MHGH27", at + timedelta(seconds=10, microseconds=1)) is None
    feed.status("disconnected", {})
    assert feed.quote("MHGH27", at) is None


def test_paper_exec_confirms_both_fills_against_rest_positions():
    paper = PaperExecution("MHGH27", Mock())
    paper.broker = Mock()
    paper.broker.pending_count.return_value = 0
    long_position = SimpleNamespace(side="LONG", qty=1)
    paper.broker.position.side_effect = [None, long_position, long_position, None]
    paper.broker.orderable_qty.return_value = 1
    paper.broker.submit.side_effect = ["1", "2"]
    paper.broker.wait_fill.side_effect = [SimpleNamespace(qty=1, price=4.01), SimpleNamespace(qty=1, price=4.02)]
    paper.broker.history.side_effect = [[{"occurs1_ord_no": "0001", "occurs1_exec_tm": "20261118091005"}],
                                       [{"occurs1_ord_no": "0002", "occurs1_exec_tm": "20261118093015"}]]
    assert paper.execute(1, 4.01, entry=True) == (datetime(2026, 11, 18, 9, 10, 5, tzinfo=KST), 4.01)
    assert paper.execute(-1, 4.02, entry=False) == (datetime(2026, 11, 18, 9, 30, 15, tzinfo=KST), 4.02)
    assert [call.args for call in paper.broker.submit.call_args_list] == [("LONG", 1), ("SHORT", 1)]
