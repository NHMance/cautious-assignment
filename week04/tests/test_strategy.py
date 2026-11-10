from dataclasses import replace
from datetime import datetime, timedelta
import pytest

from spread.strategy import Bar, PairBar, Quote, Fill, Settings, KST, quote_problem
from spread.strategy import Strategy, Signal, Position, exit_reason, entry_problem


BASE = datetime(2026, 11, 9, 9, tzinfo=KST)


def pair(i, spread, volume=1):
    t = BASE + timedelta(minutes=i * 5)
    return PairBar(Bar(t, 1445, 1445, volume), Bar(t, 1445 + spread, 1445 + spread, volume))


def warmed():
    s = Strategy(Settings())
    for i in range(24):
        assert s.observe(pair(i, 0.6 if i % 2 == 0 else 1.4))[0] is None
    return s


@pytest.mark.parametrize("spread,direction", [(1.8, -1), (0.2, 1)])
def test_current_excluded_and_both_entry_directions(spread, direction):
    signal, reason, stats = warmed().observe(pair(24, spread))
    assert reason == "SIGNAL"
    assert signal.direction == direction
    assert stats["mean"] == pytest.approx(1)
    assert stats["std"] == pytest.approx(0.4)
    assert abs(signal.z) == pytest.approx(2)


def test_beyond_stop_band_is_not_new_entry():
    assert warmed().observe(pair(24, 2.2))[1] == "BEYOND_STOP_BAND"


def test_gap_and_zero_volume_restart_warmup():
    s = warmed()
    assert s.observe(pair(25, 1.8))[1] == "WARMUP"
    assert len(s.window) == 1
    assert s.observe(pair(26, 1, 0))[1] == "NO_VOLUME"
    assert len(s.window) == 0


def test_duplicate_does_not_change_window():
    s = warmed()
    old = list(s.window)
    assert s.observe(pair(23, 99))[1] == "DUPLICATE_OR_OLD"
    assert list(s.window) == old


def test_zero_std_has_no_signal():
    s = Strategy(Settings())
    for i in range(24):
        s.observe(pair(i, 1))
    assert s.observe(pair(24, 1.8))[1] == "ZERO_STD"


def position(direction=-1):
    t = BASE.replace(hour=12)
    signal = Signal(t, direction, 1, 0.4, -direction * 2, 1 - direction * 0.8)
    return Position(signal, Fill("N", -direction, 1445, t, "N1"),
                    Fill("F", direction, 1446.8 if direction == -1 else 1445.2, t, "F1"))


def quotes(spread, t=None):
    t = t or BASE.replace(hour=12, minute=5)
    return Quote("N", t, 1445, 1445), Quote("F", t, 1445 + spread, 1445 + spread)


@pytest.mark.parametrize("direction", [-1, 1])
def test_mean_reversion_and_leg_pnl(direction):
    pos = position(direction)
    nq, fq = quotes(1)
    assert exit_reason(pos, nq, fq, nq.ts, Settings(), 0) == "MEAN_REVERSION"
    assert pos.pnl(1445, 1446) == pytest.approx(8000)


@pytest.mark.parametrize("direction,spread", [(-1, 2.1), (1, -0.1)])
def test_adverse_z_stop(direction, spread):
    pos = position(direction)
    nq, fq = quotes(spread)
    assert exit_reason(pos, nq, fq, nq.ts, Settings(), 0) == "Z_STOP"


def test_yen_loss_hold_deadline_and_daily_equity():
    pos = position()
    nq, fq = quotes(3)
    assert exit_reason(pos, nq, fq, nq.ts, Settings(), 0) == "TRADE_LOSS"
    nq, fq = quotes(1.8, BASE.replace(hour=13))
    assert exit_reason(pos, nq, fq, nq.ts, Settings(), 0) == "TIME_STOP"
    nq, fq = quotes(1.8, BASE.replace(hour=15, minute=10))
    assert exit_reason(pos, nq, fq, nq.ts, Settings(), 0) == "FORCE_CLOSE"
    nq, fq = quotes(1.9)
    assert exit_reason(pos, nq, fq, nq.ts, Settings(), -29500) == "DAILY_LOSS"


def test_liquidation_marks_bid_ask_and_four_fees():
    pos = position()
    t = BASE.replace(hour=12, minute=5)
    nq = Quote("N", t, 1444.9, 1445.1)
    fq = Quote("F", t, 1445.9, 1446.1)
    assert pos.liquidation_pnl(nq, fq, 500) == pytest.approx(4000)


@pytest.mark.parametrize("change,expected", [
    ({"ask": 1446}, "WIDE_BOOK"), ({"bid_qty": 0}, "INSUFFICIENT_DEPTH"),
    ({"ts": BASE - timedelta(seconds=11)}, "STALE_QUOTE"),
    ({"bid": float("nan")}, "INVALID_BOOK"), ({"ask": 1400}, "INVALID_BOOK"),
])
def test_entry_quote_filters(change, expected):
    n = Quote("N", BASE, 1445, 1445.1)
    f = Quote("F", BASE, 1446, 1446.1)
    assert quote_problem(replace(n, **change), f, BASE, Settings()) == expected


def test_quotes_time_skew():
    nq = Quote("N", BASE - timedelta(seconds=4), 1445, 1445.1)
    fq = Quote("F", BASE, 1446, 1446.1)
    assert quote_problem(nq, fq, BASE, Settings()) == "QUOTE_TIME_SKEW"


def test_stale_quote_is_not_hidden_by_wide_book():
    nq = Quote("N", BASE - timedelta(seconds=20), 1445, 1446, 0, 0)
    fq = Quote("F", BASE, 1446, 1446.1)
    assert quote_problem(nq, fq, BASE, Settings()) == "STALE_QUOTE"


@pytest.mark.parametrize("spread,reason", [(1, "REVERTED_BEFORE_ENTRY"), (2.2, "BEYOND_STOP_BEFORE_ENTRY"), (1.8, None)])
def test_revalidate_signal_before_entry(spread, reason):
    nq, fq = quotes(spread)
    assert entry_problem(position().signal, nq, fq, Settings()) == reason


@pytest.mark.parametrize("config", [{"entry_z": 3}, {"fee_per_leg_side": -1}, {"window": 1},
                                    {"max_entries": 0}, {"deadline": "15:05"}, {"window": 2.5}])
def test_reject_invalid_config(config):
    with pytest.raises(ValueError):
        Settings(**config)
