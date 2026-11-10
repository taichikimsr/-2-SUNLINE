"""株価データの取得。"""

import numpy as np
import pandas as pd

from universe import to_yahoo_symbol

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def fetch_yahoo(code: str, period: str = "2y") -> pd.DataFrame | None:
    """yfinance で日足を取得（株式分割・配当を調整済み）。失敗時は None。

    yfinance は Yahoo Finance の非公式ラッパーで、仕様変更で動かなくなることがある。
    """
    import yfinance as yf

    try:
        df = yf.Ticker(to_yahoo_symbol(code)).history(period=period, interval="1d", auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df[COLUMNS].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[df["Volume"] > 0]
    return df


def synthetic(code: str, n: int = 500, seed: int | None = None) -> pd.DataFrame:
    """動作確認用の架空データ（実在の株価ではない）。"""
    if seed is None:
        seed = sum((i + 1) * ord(ch) for i, ch in enumerate(code))  # 銘柄ごとに固定の乱数
    rng = np.random.default_rng(seed)
    drift = rng.normal(0.0003, 0.0008)
    ret = rng.normal(drift, 0.018, n)
    close = 1000 * np.exp(np.cumsum(ret)) * rng.uniform(0.5, 5)
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    vol = rng.lognormal(15, 0.4, n)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)
