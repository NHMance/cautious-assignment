from datetime import datetime, timedelta
from statistics import mean, stdev

from copper.replay import replay
from copper.strategy import (Bar, Costs, IndicatorSeries, Indicators, KST, SignalEdges,
                             Thresholds, calibrate, entry_problem)


def make_bars(start, closes, width=0.0005):
    return [Bar(start + timedelta(minutes=5 * i), close, close + width, close - width, close, 1)
            for i, close in enumerate(closes)]


def test_z_uses_the_previous_24_closes_and_sample_deviation():
    closes = [4 + i * 0.0005 for i in range(25)]
    bars = make_bars(datetime(2026, 11, 2, 9, tzinfo=KST), closes)
    series = IndicatorSeries()
    result = [series.push(bar) for bar in bars]
    assert result[-2].z is None
    assert result[-1].z == (closes[-1] - mean(closes[:-1])) / stdev(closes[:-1])
    assert result[-1].er == 1


def test_validation_prices_cannot_change_frozen_thresholds():
    costs = Costs(0, 1)
    baseline = make_bars(datetime(2026, 11, 2, 9, tzinfo=KST), [4 + i * 0.0005 for i in range(60)])
    validation = make_bars(datetime(2026, 11, 16, 9, tzinfo=KST), [10, 11, 9])
    symbols = dict(symbol="MHGH27", execution_symbol="MHGH27")
    assert calibrate(baseline, costs, **symbols) == calibrate(baseline + validation, costs, **symbols)


def test_filtered_signal_is_consumed_even_after_high_vol_clears():
    bars = make_bars(datetime(2026, 11, 16, 9, tzinfo=KST), [4, 4, 4])
    thresholds = Thresholds("MHGH27", "MHGH27", 0.7, 0.3, 2, {"09:00": 0.001}, 100, 300, 0, 1)
    inputs = [Indicators(4.1, 4, 0.001, 0.5, 0), Indicators(4.1, 4, 0.003, 0.8, 0),
              Indicators(4.1, 4, 0.001, 0.8, 0)]
    for variant, expected in (("C", "HIGH_VOL"), ("C-1", None)):
        edges = SignalEdges(variant)
        signals = [edges.push(bar, row, thresholds) for bar, row in zip(bars, inputs)]
        assert [len(batch) for batch in signals] == [0, 1, 0]
        signal = signals[1][0]
        assert entry_problem(variant, signal, signal.at, 0, 0, True, None, thresholds, Costs(0, 1)) == expected


def test_replay_fills_next_open_and_applies_gap_stop():
    day = datetime(2026, 11, 16, 9, tzinfo=KST)
    bars = make_bars(day, [4] * 25 + [4.001, 4.002, 3.99])
    thresholds = Thresholds("MHGH27", "MHGH27", 0.7, 0.3, 2,
                            {bar.start.strftime("%H:%M")[:3] + suffix: 0.003
                             for bar in bars for suffix in ("00", "30")}, 100, 300, 0, 1)
    portfolios, _ = replay(bars, thresholds, Costs(0, 1))
    trade = portfolios[0].trades[0]
    assert trade["entry_at"] == bars[26].start
    assert trade["entry_price"] == bars[26].open + 0.0005
    assert trade["exit_at"] == bars[27].start
    assert trade["exit_price"] == bars[27].open - 0.0005
    assert trade["reason"] == "STOP"
    assert trade["slippage"] == 2.50
