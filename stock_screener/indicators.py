"""テクニカル指標の計算。入力は Open/High/Low/Close/Volume 列を持つ日足 DataFrame。"""

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder の平滑化（RSI・ATR で使う指数平滑）。"""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain = wilder(diff.clip(lower=0), n)
    loss = wilder(-diff.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # 下落が一度もない区間は RSI=100
    return out.where(loss != 0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return wilder(tr, n)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """主要指標を列として追加した新しい DataFrame を返す。"""
    out = df.copy()
    c = out["Close"]
    out["MA5"] = sma(c, 5)
    out["MA25"] = sma(c, 25)
    out["MA75"] = sma(c, 75)
    out["MA25_slope"] = out["MA25"].pct_change(5)  # 5営業日前比の傾き
    out["RSI"] = rsi(c, 14)
    macd_line = ema(c, 12) - ema(c, 26)
    out["MACD"] = macd_line
    out["MACD_signal"] = ema(macd_line, 9)
    out["MACD_hist"] = out["MACD"] - out["MACD_signal"]
    out["ATR"] = atr(out, 14)
    out["ATR_pct"] = out["ATR"] / c
    out["Dev25"] = c / out["MA25"] - 1  # 25日線からの乖離率
    out["Vol5"] = sma(out["Volume"], 5)
    out["Vol25"] = sma(out["Volume"], 25)
    out["VolRatio"] = out["Vol5"] / out["Vol25"]
    out["TurnoverAvg"] = sma(c * out["Volume"], 25)  # 25日平均売買代金（円）
    mid = sma(c, 20)
    sd = c.rolling(20, min_periods=20).std()
    out["BB_upper"] = mid + 2 * sd
    out["BB_lower"] = mid - 2 * sd
    return out
