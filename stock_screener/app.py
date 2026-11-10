"""日本株 短期スクリーナー（Streamlit）。

起動: streamlit run app.py
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from data import fetch_yahoo, synthetic
from indicators import add_indicators
from strategy import Params, analyze, rank
from universe import DEMO_UNIVERSE, UNIVERSE

st.set_page_config(page_title="日本株 短期スクリーナー", layout="wide")


@st.cache_data(ttl=3600, show_spinner=False)
def load(code: str, demo: bool) -> pd.DataFrame | None:
    return synthetic(code) if demo else fetch_yahoo(code)


def chart(df: pd.DataFrame, plan: dict, title: str) -> go.Figure:
    d = add_indicators(df).iloc[-120:]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.6, 0.2, 0.2], vertical_spacing=0.03)
    fig.add_trace(
        go.Candlestick(x=d.index, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
                       name="株価", increasing_line_color="#d62728", decreasing_line_color="#1f77b4"),
        row=1, col=1,
    )
    for col, color in (("MA5", "#999"), ("MA25", "#ff7f0e"), ("MA75", "#2ca02c")):
        fig.add_trace(go.Scatter(x=d.index, y=d[col], name=col, line=dict(width=1, color=color)), row=1, col=1)
    for label, color in (("利益確定の目安", "#2ca02c"), ("買い目安（翌営業日の寄付き付近）", "#555"),
                         ("損切りライン", "#d62728")):
        fig.add_hline(y=plan[label], line_dash="dash", line_color=color, row=1, col=1,
                      annotation_text=label.split("（")[0], annotation_position="top left")
    fig.add_trace(go.Bar(x=d.index, y=d["MACD_hist"], name="MACDヒストグラム", marker_color="#888"), row=2, col=1)
    fig.add_trace(go.Scatter(x=d.index, y=d["MACD"], name="MACD", line=dict(width=1)), row=2, col=1)
    fig.add_trace(go.Scatter(x=d.index, y=d["MACD_signal"], name="シグナル", line=dict(width=1)), row=2, col=1)
    fig.add_trace(go.Scatter(x=d.index, y=d["RSI"], name="RSI", line=dict(width=1, color="#9467bd")), row=3, col=1)
    for y in (30, 70):
        fig.add_hline(y=y, line_dash="dot", line_color="#bbb", row=3, col=1)
    fig.update_layout(title=title, height=650, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=40, b=10),
                      legend=dict(orientation="h", y=-0.05))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig


st.title("日本株 短期スクリーナー")
st.caption(
    "テクニカル指標のルールで条件の良い銘柄を抽出し、同じルールの過去成績を併記します。"
    "将来の値動きを予測・保証するものではありません。"
)

with st.sidebar:
    st.header("設定")
    demo = st.toggle("デモモード（架空データ）", value=False)
    top_n = st.number_input("表示銘柄数", 1, 20, 5)
    st.subheader("売買ルール")
    stop_atr = st.slider("損切り幅（ATR×）", 1.0, 4.0, 2.0, 0.5)
    target_atr = st.slider("利確幅（ATR×）", 1.0, 6.0, 3.0, 0.5)
    max_hold = st.slider("最大保有日数", 3, 30, 10)
    cost = st.number_input("往復コスト想定（%）", 0.0, 2.0, 0.2, 0.05) / 100
    min_to = st.number_input("最低売買代金（億円/日）", 0.0, 1000.0, 10.0, 5.0) * 1e8
    extra = st.text_input("追加銘柄コード（カンマ区切り）", placeholder="例: 7203,6758")
    run = st.button("分析する", type="primary", width="stretch")

p = Params(stop_atr=stop_atr, target_atr=target_atr, max_hold=max_hold, cost_pct=cost, min_turnover=min_to)

if demo:
    st.warning("デモモード：表示されるのは架空データです。実在の銘柄・株価ではありません。")

if run:
    universe = dict(DEMO_UNIVERSE if demo else UNIVERSE)
    if not demo:
        for c in (x.strip() for x in extra.split(",")):
            if c and c not in universe:
                universe[c] = c
    results, failed, raw = [], [], {}
    bar = st.progress(0.0, text="データ取得中…")
    for i, (code, name) in enumerate(universe.items(), 1):
        df = load(code, demo)
        r = analyze(code, name, df, p)
        if r:
            results.append(r)
            raw[code] = df
        else:
            failed.append(code)
        bar.progress(i / len(universe), text=f"データ取得中… {i}/{len(universe)}")
    bar.empty()
    st.session_state["out"] = (results, failed, raw, p)

if "out" not in st.session_state:
    st.info("左の「分析する」を押してください。")
    st.stop()

results, failed, raw, p_used = st.session_state["out"]
if failed:
    st.caption(f"取得・計算できなかった銘柄: {', '.join(failed)}")
if not results:
    st.error("データを取得できませんでした。ネットワーク接続を確認してください。")
    st.stop()

top = rank(results, int(top_n), p_used)
base = max(r.last_date for r in results)
st.subheader(f"おすすめ候補 上位{len(top)}銘柄（基準日 {base:%Y-%m-%d} 終値）")

summary = pd.DataFrame(
    [
        {
            "順位": i,
            "コード": r.code,
            "銘柄名": r.name,
            "スコア": r.score,
            "買いシグナル": "点灯" if r.entry_signal else "未点灯",
            "終値": round(r.close, 1),
            "損切り": r.plan["損切りライン"],
            "利確目安": r.plan["利益確定の目安"],
            "過去取引数": r.backtest.get("取引回数", 0),
            "過去勝率": f"{r.backtest['勝率']:.0%}" if r.backtest.get("取引回数") else "-",
            "過去平均損益": f"{r.backtest['平均損益']:+.2%}" if r.backtest.get("取引回数") else "-",
        }
        for i, r in enumerate(top, 1)
    ]
)
st.dataframe(summary, hide_index=True, width="stretch")

for i, r in enumerate(top, 1):
    with st.expander(f"{i}. {r.code} {r.name}　スコア {r.score}/100", expanded=(i == 1)):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**根拠**")
            st.markdown("\n".join(f"- {s}" for s in r.reasons) or "-")
            if r.warnings:
                st.markdown("**注意点**")
                st.markdown("\n".join(f"- {s}" for s in r.warnings))
        with c2:
            st.markdown("**売買の目安**")
            if not r.entry_signal:
                st.caption("買いシグナル未点灯のため、点灯までは見送りが基本（以下は参考値）。")
            st.table(pd.DataFrame([(k, f"{v:,.1f}円" if isinstance(v, float) else str(v)) for k, v in r.plan.items()],
                                  columns=["項目", "値"]).set_index("項目"))
            st.markdown(
                "**売り（手仕舞い）の条件**\n"
                "- 損切りラインに到達\n- 利益確定の目安に到達\n"
                "- 終値が25日線を下回る\n"
                f"- 保有{p_used.max_hold}営業日経過"
            )
        with c3:
            st.markdown("**同じルールの過去成績（約2年）**")
            bt = r.backtest
            if bt.get("取引回数"):
                st.table(pd.DataFrame(
                    {
                        "値": [
                            f"{bt['取引回数']}回", f"{bt['勝率']:.0%}", f"{bt['平均損益']:+.2%}",
                            f"{bt['平均利益']:+.2%}", f"{bt['平均損失']:+.2%}",
                            f"{bt['プロフィットファクター']:.2f}", f"{bt['平均保有日数']:.1f}日",
                        ]
                    },
                    index=["取引回数", "勝率", "平均損益", "平均利益", "平均損失", "プロフィットファクター", "平均保有日数"],
                ))
                if bt["取引回数"] < 10:
                    st.caption("取引回数が少ないため、成績の信頼性は低い。")
            else:
                st.caption("過去に同条件の取引なし。")
        st.plotly_chart(chart(raw[r.code], r.plan, f"{r.code} {r.name}（直近120営業日）"), width="stretch")

st.divider()
st.caption(
    "【免責】本アプリは情報提供・学習目的のツールであり、投資助言ではありません。"
    "株価データは Yahoo Finance（yfinance 経由）から取得しており、遅延・欠損・誤りがあり得ます。"
    "投資判断はご自身の責任で行ってください。"
)
