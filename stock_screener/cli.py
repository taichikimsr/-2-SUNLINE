"""ターミナルで上位銘柄を表示する。

使い方:
    python cli.py            # 実データ（yfinance）
    python cli.py --demo     # 架空データで動作確認
"""

import argparse

from data import fetch_yahoo, synthetic
from strategy import Params, analyze, rank
from universe import DEMO_UNIVERSE, UNIVERSE


def fmt_pct(x: float) -> str:
    return f"{x:+.2%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="架空データで実行")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    p = Params()
    results, failed = [], []
    universe = DEMO_UNIVERSE if args.demo else UNIVERSE
    for code, name in universe.items():
        df = synthetic(code) if args.demo else fetch_yahoo(code)
        r = analyze(code, name, df, p)
        (results if r else failed).append(r or code)

    if args.demo:
        print("※ デモモード：架空データです。実在の銘柄の株価ではありません。\n")
    if failed:
        print(f"取得・計算できなかった銘柄: {', '.join(failed)}\n")

    for i, r in enumerate(rank(results, args.top, p), 1):
        sig = "買いシグナル点灯" if r.entry_signal else "シグナル未点灯（監視候補）"
        print(f"{i}. {r.code} {r.name}  スコア {r.score}/100  [{sig}]")
        print(f"   基準日 {r.last_date:%Y-%m-%d}  終値 {r.close:,.1f}円")
        print("   根拠:")
        for s in r.reasons:
            print(f"     ・{s}")
        if r.warnings:
            print("   注意:")
            for s in r.warnings:
                print(f"     ・{s}")
        print("   売買の目安:" + ("" if r.entry_signal else "（シグナル点灯までは見送り。参考値）"))
        for k, v in r.plan.items():
            print(f"     {k}: {v}")
        bt = r.backtest
        if bt.get("取引回数", 0):
            print(
                f"   過去の同条件の成績: {bt['取引回数']}回 勝率 {bt['勝率']:.0%} "
                f"平均 {fmt_pct(bt['平均損益'])} PF {bt['プロフィットファクター']:.2f}"
            )
        else:
            print("   過去の同条件の成績: 該当なし")
        print()

    print("本出力は過去データに基づく機械的な抽出で、将来の値動きを保証するものではありません。")


if __name__ == "__main__":
    main()
