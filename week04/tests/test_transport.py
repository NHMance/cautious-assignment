"""네트워크 시간 초과 안내와 중단 기록을 실제 접속 없이 확인합니다."""
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from spread.journal import Journal
from backend.nh_api.catalog import HTTP_APIS, Limit
from backend.nh_api.client import TradeClient
from backend.nh_api.errors import TransportError
from spread import market
from spread.strategy import KST, Settings


def client_for(http):
    session = SimpleNamespace(
        config=SimpleNamespace(default_environment="paper", market=lambda *args: None),
        tokens=SimpleNamespace(headers=lambda *args: {}),
        client=http,
        base_url=lambda env: "https://example.invalid",
        close=http.close,
    )
    return TradeClient(session, limiter=SimpleNamespace(wait=lambda *args: None))


@pytest.mark.parametrize("api_id", [
    api_id for api_id, spec in HTTP_APIS.items()
    if spec.limit == Limit.ORDER or api_id in {"domestic_price-bid", "overseas_bid-price", "domestic_open-interest"}
])
def test_timeout_distinguishes_queries_from_orders_without_resubmitting(api_id):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("test timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
        api = client_for(http)
        with pytest.raises(TransportError) as error:
            api.call(api_id, {"sym": "TEST"})

    assert len(calls) == 1
    assert error.value.api_id == api_id
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    if HTTP_APIS[api_id].limit == Limit.ORDER:
        assert "주문내역이나 잔고로 확인" in str(error.value)
    else:
        assert "다시 조회" in str(error.value)
        assert "주문내역" not in str(error.value)


def test_halt_prints_api_and_timeout_kind(tmp_path, capsys):
    journal = Journal(tmp_path, "paper", echo=True)
    journal.write("HALT", error_kind="TransportError", api="domestic_price-bid",
                  cause="ReadTimeout", detail="조회 응답 없음", manual_reconciliation=False)
    output = capsys.readouterr().out
    assert "domestic_price-bid" in output
    assert "ReadTimeout" in output
    row = json.loads(journal.path.read_text())
    assert row["manual_reconciliation"] is False


@pytest.mark.parametrize("failures", [0, 1, 3])
def test_quote_recovers_within_three_retries(tmp_path, monkeypatch, failures):
    requests, delays = [], []

    def respond(request):
        requests.append(request)
        if len(requests) <= failures:
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(200, json={
            "sym": "TEST", "qt_tm": datetime.now(KST).strftime("%H%M%S"),
            "Occurs1": [{"occurs1_ask_best_pric": "1445.1", "occurs1_bid_best_pric": "1445.0",
                         "occurs1_ask_qty": "10", "occurs1_bid_qty": "20"}],
        })

    monkeypatch.setattr(market.time, "sleep", delays.append)
    journal = Journal(tmp_path, "paper")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        result = market.quote(client_for(http), "TEST", journal)
    assert result.bid == 1445.0 and result.ask == 1445.1
    assert len(requests) == failures + 1
    assert all(r.url.path == "/trade/v1/domestic/price-bid" for r in requests)
    assert delays == [1.0] * failures
    rows = [json.loads(s) for s in journal.path.read_text().splitlines()]
    assert [r["type"] for r in rows] == ["QUOTE_RETRY"] * failures + ["API_RESPONSE"]


@pytest.mark.parametrize("timeout_kind", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_paper_halts_after_quote_retries_without_ordering(tmp_path, monkeypatch, timeout_kind):
    import main
    requests, delays = [], []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 12, 12, tzinfo=KST)

    def timeout(request):
        requests.append(request)
        raise timeout_kind("test timeout", request=request)

    monkeypatch.setattr(market.time, "sleep", delays.append)
    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "verify_prices", lambda *args: None)
    monkeypatch.setattr(main, "fetch_bars", lambda *args: [])
    monkeypatch.setattr(main, "PaperBroker", lambda *args: SimpleNamespace(assert_positions=lambda expected: None))
    with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
        monkeypatch.setattr(main, "create_client", lambda env_file: client_for(http))
        with pytest.raises(TransportError) as error:
            main.run_paper({"near": {"series": "N"}, "far": {"series": "F"}}, Settings(), tmp_path / "log")

    assert error.value.api_id == "domestic_price-bid"
    assert len(requests) == 4
    assert all(r.url.path == "/trade/v1/domestic/price-bid" for r in requests)
    assert delays == [1.0] * 3
    rows = [json.loads(s) for s in next((tmp_path / "log").glob("journal_*.jsonl")).read_text().splitlines()]
    assert [r["type"] for r in rows] == [
        "RUN_START", "DATA_ALIGNMENT", "QUOTE_RETRY", "QUOTE_RETRY", "QUOTE_RETRY",
        "QUOTE_RETRY_EXHAUSTED", "HALT", "RUN_END",
    ]
    assert rows[-2]["api"] == "domestic_price-bid"
    assert rows[-2]["cause"] == timeout_kind.__name__
    assert not (tmp_path / "results/private/paper.lock").exists()


@pytest.mark.parametrize("error_kind", [ValueError, TransportError])
def test_quote_does_not_retry_parsing_or_auth_failures(tmp_path, monkeypatch, error_kind):
    from backend.domestic_futureoption import domestic_futureoption_functions as nh
    calls, delays = [], []
    error = (TransportError("domestic_price-bid", "인증 실패") if error_kind is TransportError
             else ValueError("응답 오류"))

    def fail(*args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(nh, "price_bid", fail)
    monkeypatch.setattr(market.time, "sleep", delays.append)
    with pytest.raises(error_kind):
        market.quote(None, "TEST", Journal(tmp_path, "paper"))
    assert len(calls) == 1
    assert not delays
