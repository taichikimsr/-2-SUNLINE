"""スコアリング・売買目安・バックテスト。

方針: 「予測」ではなく、事前に決めたルールで条件を満たす銘柄を抽出し、
同じルールを過去データに当てはめた成績（バックテスト）を根拠として併記する。
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from indicators import add_indicators

MIN_BARS = 120  # 指標計算に必要な最低本数（MA75 + 余裕）


@dataclass
class Params:
    stop_atr: float = 2.0  # 損切り幅 = ATR × この値
    target_atr: float = 3.0  # 利確幅 = ATR × この値
    max_hold: int = 10  # 最大保有営業日数（時間切れ決済）
    cost_pct: float = 0.002  # 往復の取引コスト・スリッページ想定（0.2%）
    min_turnover: float = 1e9  # 25日平均売買代金の下限（円）


@dataclass
class Result:
    code: str
    name: str
    score: int
    last_date: pd.Timestamp
    close: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entry_signal: bool = False
    plan: dict = field(default_factory=dict)
    backtest: dict = field(default_factory=dict)


def entry_signal_series(d: pd.DataFrame) -> pd.Series:
    """バックテストと当日判定で共通に使う「買いシグナル」条件。"""
    cond = (
        (d["Close"] > d["MA25"])
        & (d["MA25"] > d["MA75"])
        & (d["MA25_slope"] > 0)
        & (d["MACD"] > d["MACD_signal"])
        & (d["MACD_hist"] > d["MACD_hist"].shift(1))
        & d["RSI"].between(45, 70)
        & d["Dev25"].between(0, 0.08)
        & (d["VolRatio"] >= 1.0)
    )
    return cond.fillna(False)


def score_last(d: pd.DataFrame) -> tuple[int, list[str], list[str]]:
    """最新足を 100 点満点で採点し、根拠と注意点を返す。"""
    r = d.iloc[-1]
    pts = 0
    reasons: list[str] = []
    warns: list[str] = []

    # 1. トレンド（25点）
    if r["Close"] > r["MA75"]:
        pts += 10
        reasons.append("終値が75日線を上回る（中期上昇基調）")
    if r["MA25"] > r["MA75"]:
        pts += 10
        reasons.append("25日線が75日線より上（短中期の並びが上向き）")
    if r["MA25_slope"] > 0:
        pts += 5
        reasons.append(f"25日線が上向き（5日前比 {r['MA25_slope']:+.2%}）")
    else:
        warns.append("25日線が下向き")

    # 2. モメンタム（25点）
    if r["MACD"] > r["MACD_signal"]:
        pts += 10
        reasons.append("MACDがシグナル線より上")
    else:
        warns.append("MACDがシグナル線より下")
    hist = d["MACD_hist"].iloc[-3:]
    if len(hist) == 3 and hist.is_monotonic_increasing:
        pts += 5
        reasons.append("MACDヒストグラムが3日連続で拡大")
    cross = (d["MACD"] > d["MACD_signal"]) & (d["MACD"].shift(1) <= d["MACD_signal"].shift(1))
    if cross.iloc[-5:].any():
        pts += 10
        reasons.append("直近5営業日以内にMACDゴールデンクロス")

    # 3. RSI（15点）
    rv = r["RSI"]
    if 45 <= rv <= 65:
        pts += 15
        reasons.append(f"RSI {rv:.1f}（過熱感なく上昇余地あり）")
    elif 40 <= rv < 45 or 65 < rv <= 70:
        pts += 8
        reasons.append(f"RSI {rv:.1f}")
    elif rv > 75:
        warns.append(f"RSI {rv:.1f}（買われ過ぎ）")
    elif rv < 30:
        warns.append(f"RSI {rv:.1f}（売られ過ぎ・下落基調の可能性）")

    # 4. 25日線乖離（15点）
    dv = r["Dev25"]
    if 0 <= dv <= 0.05:
        pts += 15
        reasons.append(f"25日線乖離 {dv:+.1%}（押し目圏・高値追いでない）")
    elif 0.05 < dv <= 0.10:
        pts += 5
        reasons.append(f"25日線乖離 {dv:+.1%}")
    elif dv > 0.10:
        warns.append(f"25日線乖離 {dv:+.1%}（短期的に上がり過ぎ）")

    # 5. 出来高（10点）
    vr = r["VolRatio"]
    if vr >= 1.3:
        pts += 10
        reasons.append(f"出来高増加（5日平均が25日平均の {vr:.2f} 倍）")
    elif vr >= 1.0:
        pts += 5
        reasons.append(f"出来高は平常以上（{vr:.2f} 倍）")

    # 6. 値動きの大きさ（10点）
    ap = r["ATR_pct"]
    if 0.01 <= ap <= 0.04:
        pts += 10
        reasons.append(f"1日の平均値幅 {ap:.1%}（短期売買に適度）")
    elif ap > 0.04:
        warns.append(f"1日の平均値幅 {ap:.1%}（値動きが荒い）")

    return pts, reasons, warns


def exit_warnings(d: pd.DataFrame) -> list[str]:
    """保有中に「売り」を検討すべき状態を返す。"""
    r = d.iloc[-1]
    out = []
    if r["Close"] < r["MA25"]:
        out.append("終値が25日線を割り込んだ（手仕舞い条件）")
    dead = (d["MACD"] < d["MACD_signal"]) & (d["MACD"].shift(1) >= d["MACD_signal"].shift(1))
    if dead.iloc[-3:].any():
        out.append("直近3営業日以内にMACDデッドクロス")
    if r["RSI"] > 75:
        out.append("RSIが75超（利益確定を検討）")
    if r["Close"] > r["BB_upper"]:
        out.append("ボリンジャーバンド+2σ超え（短期過熱）")
    return out


def trade_plan(d: pd.DataFrame, p: Params) -> dict:
    """最新足を基準にした売買の目安。"""
    r = d.iloc[-1]
    close, a = float(r["Close"]), float(r["ATR"])
    entry = close
    pullback = max(float(r["MA5"]), close - 0.5 * a)
    pullback = min(pullback, close)
    stop = entry - p.stop_atr * a
    target = entry + p.target_atr * a
    return {
        "買い目安（翌営業日の寄付き付近）": round(entry, 1),
        "押し目買い指値の目安": round(pullback, 1),
        "損切りライン": round(stop, 1),
        "利益確定の目安": round(target, 1),
        "損切り幅": f"{-p.stop_atr * a / entry:.1%}",
        "利確幅": f"{p.target_atr * a / entry:+.1%}",
        "最大保有期間": f"{p.max_hold}営業日",
    }


def backtest(d: pd.DataFrame, p: Params) -> dict:
    """同じ買いシグナルを過去に適用した場合の成績。

    ルール: シグナル翌日の始値で買い。損切り・利確は ATR 基準。
    同じ日に損切りと利確の両方に届いた場合は損切りを優先（保守的）。
    終値が25日線を割るか、最大保有日数に達したら終値で決済。
    """
    sig = entry_signal_series(d).to_numpy()
    o, h, l, c = (d[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    ma25, at = d["MA25"].to_numpy(), d["ATR"].to_numpy()
    n = len(d)
    rets: list[float] = []
    holds: list[int] = []
    i = 0
    while i < n - 1:
        if not sig[i] or np.isnan(at[i]):
            i += 1
            continue
        entry = o[i + 1]
        stop = entry - p.stop_atr * at[i]
        target = entry + p.target_atr * at[i]
        exit_px, j = None, i + 1
        while j < n:
            if o[j] <= stop and j > i + 1:
                exit_px = o[j]
            elif l[j] <= stop:
                exit_px = stop
            elif o[j] >= target and j > i + 1:
                exit_px = o[j]
            elif h[j] >= target:
                exit_px = target
            elif c[j] < ma25[j] or (j - i) >= p.max_hold:
                exit_px = c[j]
            if exit_px is not None:
                break
            j += 1
        if exit_px is None:  # データ末尾で未決済 → 集計対象外
            break
        rets.append(exit_px / entry - 1 - p.cost_pct)
        holds.append(j - i)
        i = j + 1

    if not rets:
        return {"取引回数": 0}
    r = np.array(rets)
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    return {
        "取引回数": len(r),
        "勝率": float((r > 0).mean()),
        "平均損益": float(r.mean()),
        "平均利益": float(wins.mean()) if len(wins) else 0.0,
        "平均損失": float(losses.mean()) if len(losses) else 0.0,
        "プロフィットファクター": float(pf),
        "累積損益（単利合計）": float(r.sum()),
        "平均保有日数": float(np.mean(holds)),
    }


def analyze(code: str, name: str, raw: pd.DataFrame, p: Params | None = None) -> Result | None:
    p = p or Params()
    if raw is None or len(raw) < MIN_BARS:
        return None
    d = add_indicators(raw)
    if d[["MA75", "ATR", "RSI", "MACD_signal"]].iloc[-1].isna().any():
        return None
    pts, reasons, warns = score_last(d)
    if d["TurnoverAvg"].iloc[-1] < p.min_turnover:
        warns.append(f"売買代金が少ない（25日平均 {d['TurnoverAvg'].iloc[-1] / 1e8:.1f}億円）")
    warns += exit_warnings(d)
    return Result(
        code=code,
        name=name,
        score=pts,
        last_date=d.index[-1],
        close=float(d["Close"].iloc[-1]),
        reasons=reasons,
        warnings=warns,
        entry_signal=bool(entry_signal_series(d).iloc[-1]),
        plan=trade_plan(d, p),
        backtest=backtest(d, p),
    )


def rank(results: list[Result], top: int = 5, p: Params | None = None) -> list[Result]:
    """上位銘柄を選ぶ。流動性不足と過去成績がマイナスの銘柄は除外する。"""
    p = p or Params()
    ok = []
    for r in results:
        if r is None:
            continue
        if any(w.startswith("売買代金が少ない") for w in r.warnings):
            continue
        bt = r.backtest
        if bt.get("取引回数", 0) >= 3 and bt["平均損益"] <= 0:
            continue
        ok.append(r)
    ok.sort(
        key=lambda r: (r.entry_signal, r.score, r.backtest.get("平均損益", 0.0)),
        reverse=True,
    )
    return ok[:top]
