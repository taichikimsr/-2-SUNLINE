import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import strategy  # noqa: E402
from data import synthetic  # noqa: E402
from indicators import add_indicators, rsi  # noqa: E402
from strategy import Params, analyze, backtest, rank  # noqa: E402


def test_rsi_all_up_is_100():
    s = pd.Series(np.arange(1, 50, dtype=float))
    assert rsi(s).iloc[-1] == pytest.approx(100.0)


def test_rsi_all_down_is_0():
    s = pd.Series(np.arange(50, 1, -1, dtype=float))
    assert rsi(s).iloc[-1] == pytest.approx(0.0)


def _frame(rows):
    """rows: (open, high, low, close) のリスト。ATR=10, MA25=0 固定。"""
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"])
    df["MA25"] = 0.0
    df["ATR"] = 10.0
    return df


def _signal_at_first(monkeypatch):
    monkeypatch.setattr(
        strategy, "entry_signal_series", lambda d: pd.Series([True] + [False] * (len(d) - 1), index=d.index)
    )


P = Params(stop_atr=2, target_atr=3, max_hold=10, cost_pct=0.0)


def test_backtest_target_hit(monkeypatch):
    _signal_at_first(monkeypatch)
    d = _frame([(100, 100, 100, 100), (100, 131, 99, 120), (120, 120, 120, 120)])
    bt = backtest(d, P)  # 買い100、利確130
    assert bt["取引回数"] == 1
    assert bt["平均損益"] == pytest.approx(0.30)


def test_backtest_stop_has_priority_when_both_hit(monkeypatch):
    _signal_at_first(monkeypatch)
    d = _frame([(100, 100, 100, 100), (100, 140, 70, 100), (100, 100, 100, 100)])
    bt = backtest(d, P)  # 同日に両方 → 損切り80を優先
    assert bt["平均損益"] == pytest.approx(-0.20)


def test_backtest_gap_down_exits_at_open(monkeypatch):
    _signal_at_first(monkeypatch)
    d = _frame([(100, 100, 100, 100), (100, 101, 99, 100), (60, 65, 55, 62), (62, 62, 62, 62)])
    bt = backtest(d, P)  # 損切り80より下で寄り付き → 始値60で決済
    assert bt["平均損益"] == pytest.approx(-0.40)


def test_backtest_time_stop(monkeypatch):
    _signal_at_first(monkeypatch)
    rows = [(100, 100, 100, 100)] + [(100, 101, 99, 101)] * 12
    bt = backtest(_frame(rows), P)
    assert bt["平均保有日数"] == 10
    assert bt["平均損益"] == pytest.approx(0.01)


def test_backtest_cost_applied(monkeypatch):
    _signal_at_first(monkeypatch)
    d = _frame([(100, 100, 100, 100), (100, 131, 99, 120), (120, 120, 120, 120)])
    bt = backtest(d, Params(stop_atr=2, target_atr=3, cost_pct=0.002))
    assert bt["平均損益"] == pytest.approx(0.298)


def test_analyze_and_rank_on_synthetic():
    res = [analyze(c, c, synthetic(c)) for c in (f"DEMO{i:02d}" for i in range(1, 21))]
    assert all(r is not None for r in res)
    for r in res:
        assert 0 <= r.score <= 100
        plan = r.plan
        assert plan["損切りライン"] < plan["買い目安（翌営業日の寄付き付近）"] < plan["利益確定の目安"]
    top = rank(res, 5)
    assert len(top) <= 5
    scores = [(r.entry_signal, r.score) for r in top]
    assert scores == sorted(scores, reverse=True)


def test_analyze_returns_none_for_short_data():
    assert analyze("X", "X", synthetic("X", n=50)) is None


def test_indicators_no_lookahead():
    """未来のデータを追加しても過去の指標値が変わらないこと。"""
    full = synthetic("DEMO01", n=300)
    a = add_indicators(full.iloc[:200])
    b = add_indicators(full).iloc[:200]
    cols = ["MA25", "MA75", "RSI", "MACD", "ATR", "VolRatio"]
    pd.testing.assert_frame_equal(a[cols], b[cols])
