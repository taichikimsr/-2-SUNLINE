"""広告適性の判定と、月予算の配分

実際の運用代行と同じ考え方で、次の順に決める。

1. 広告適性スコア（0〜100点）
   採算性（40点）: 1クリックで見込める粗利が、キーワードCPCの下限に対して十分か
   価格（15点）  : 低価格の商品は1件あたりの粗利が小さく、クリック課金と相性が悪い
   需要（25点）  : ブランド・作品名などの指名需要、検索キーワードデータの流入、RPPでの受注実績
   季節（10点）  : 今の時期に需要が上がる商品か（年末のギフト・玩具、冬の保湿 など）
   実績（±）     : RPPの実績があれば、目標ROASに対する成績で加点・減点
   → 60点以上「適している」、40〜59点「テストで見極め」、40点未満「適さない」

2. 月予算の配分（優先度の高い順に予算を割り当て、予算の9割で打ち止め。1割は変動の余裕）
   ① 目標ROASを達成している既存の配信（実績のある勝ち筋）
   ② 採算の合う商品への新規キーワード
   ③ 改善中の既存の配信（CPCを下げて継続するもの）
   ④ 広告適性の高い順に、実績づくりのテスト配信（商品CPC × 判定に必要なクリック数）

   予算が付かなかった商品と、広告に適さない商品は、RPPの除外商品に登録する。
   RPPは初期状態で全商品がキャンペーンCPCで配信されるため、除外しないと予算を割り当てていない商品にも費用が流れる。

月額の見込み費用はレポート期間の実績（1日あたり）×30日 で計算した目安であり、実際の配信量は楽天側の表示機会で変わる。
上限を確実に守るには、RMSのキャンペーン設定で「月予算」を同じ金額にする。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .keywords import tokenize

# 時期によって需要が変わる語（月 → 語）
SEASON_UP = {
    (10, 11, 12): {"フィギュア", "プラモデル", "おもちゃ", "玩具", "ブロック", "知育玩具", "ぬいぐるみ", "ゲーム",
                   "クリスマス", "プレゼント", "ギフト", "コレクション"},
    (11, 12, 1, 2): {"保湿", "乾燥", "乾燥肌", "リップ", "リップクリーム", "ハンドクリーム", "加湿", "防寒", "保温",
                     "ニット", "手袋", "マフラー", "カイロ"},
    (3, 4, 5, 6, 7, 8): {"日焼け止め", "日傘", "uvカット", "サンダル", "冷感", "接触冷感", "扇風機", "虫除け", "水着"},
}
SEASON_DOWN = {
    (10, 11, 12, 1, 2): {"日焼け止め", "日傘", "冷感", "接触冷感", "扇風機", "サンダル", "水着", "虫除け"},
    (5, 6, 7, 8): {"カイロ", "防寒", "ニット", "マフラー", "手袋"},
}

SUITABLE = "適している"
TRIAL = "テストで見極め"
UNSUITABLE = "適さない"


def _season(name: str, month: int):
    words = {t.lower() for t in tokenize(name)}
    low = name.lower()
    for months, ws in SEASON_UP.items():
        if month in months and any(w in words or w in low for w in ws):
            return 10, "今の時期に需要が上がる商品"
    for months, ws in SEASON_DOWN.items():
        if month in months and any(w in words or w in low for w in ws):
            return -10, "今の時期は需要が下がる商品"
    return 0, ""


def suitability(universe: pd.DataFrame, s, today: Optional[date] = None,
                search_kw: Optional[pd.DataFrame] = None, brand_terms: Optional[set] = None) -> pd.DataFrame:
    today = today or date.today()
    brand_terms = {b.lower() for b in (brand_terms or set())}
    access_by_item: Dict[str, float] = {}
    if search_kw is not None and not search_kw.empty and "アクセス数" in search_kw:
        from .keywords import relevance
        for _, q in search_kw.sort_values("アクセス数", ascending=False).head(300).iterrows():
            for _, u in universe.iterrows():
                if relevance(q["キーワード"], str(u["商品名"])) >= 0.999:
                    access_by_item[u["商品管理番号"]] = access_by_item.get(u["商品管理番号"], 0) + float(q["アクセス数"] or 0)

    rows = []
    for _, u in universe.iterrows():
        reasons: List[str] = []
        price = u.get("客単価")
        price = float(price) if price is not None and not pd.isna(price) else np.nan
        margin = float(u.get("粗利率") or s.default_margin)
        vpc = u.get("1クリック売上")
        vpc = float(vpc) if vpc is not None and not pd.isna(vpc) else np.nan
        be_cpc = vpc * margin / 100.0 if not math.isnan(vpc) else np.nan  # 損益分岐CPC

        # 採算性（40）
        if math.isnan(be_cpc):
            econ = 10.0
            reasons.append("価格不明のため採算は推定できず")
        else:
            econ = 40.0 * min(be_cpc / s.min_kw_cpc, 1.5) / 1.5
            reasons.append(f"損益分岐CPC {be_cpc:.0f}円（キーワード下限{s.min_kw_cpc}円）")
        # 価格（15）
        if math.isnan(price):
            pr = 5.0
        elif price >= 3000:
            pr = 15.0
        elif price >= 1500:
            pr = 10.0
        elif price >= 1000:
            pr = 5.0
        else:
            pr = 0.0
            reasons.append(f"低価格（{price:,.0f}円）でクリック課金と相性が悪い")
        # 需要（25）
        dem = 0.0
        toks = {t.lower() for t in tokenize(str(u["商品名"]))}
        if toks & brand_terms:
            dem += 10
            reasons.append("ブランド・作品名の指名需要あり")
        acc = access_by_item.get(u["商品管理番号"], 0)
        if acc > 0:
            dem += min(10.0, 2.5 * math.log1p(acc))
            reasons.append(f"検索流入{acc:.0f}件")
        orders = float(u.get("売上件数") or 0)
        if orders > 0:
            dem += 5
            reasons.append(f"RPPで{orders:.0f}件受注")
        # 季節（±10）
        se, se_reason = _season(str(u["商品名"]), today.month)
        if se_reason:
            reasons.append(se_reason)
        # 実績（±）
        perf = 0.0
        clicks = float(u.get("クリック数") or 0)
        if clicks >= s.min_clicks:
            roas = u.get("ROAS(%)")
            target = float(u.get("目標ROAS(%)") or 0)
            if roas is not None and not pd.isna(roas) and target > 0:
                if roas >= target:
                    perf = 20
                    reasons.append(f"実績ROAS {roas:.0f}%で目標達成")
                elif roas < target * 0.5:
                    perf = -25
                    reasons.append(f"実績ROAS {roas:.0f}%で目標の半分未満")
                else:
                    perf = -5
        score = max(0.0, min(100.0, econ + pr + dem + se + perf))
        verdict = SUITABLE if score >= 60 else TRIAL if score >= 40 else UNSUITABLE
        rows.append({"商品管理番号": u["商品管理番号"], "広告適性": round(score), "適性判定": verdict,
                     "損益分岐CPC": None if math.isnan(be_cpc) else round(be_cpc, 1), "適性の理由": "／".join(reasons)})
    return pd.DataFrame(rows)


def allocate_budget(plan_actions: pd.DataFrame, universe: pd.DataFrame, suit: pd.DataFrame, kw_stats: pd.DataFrame,
                    items: pd.DataFrame, s, monthly_budget: float, period_days: Optional[int]):
    """月予算を優先度順に配分する。戻り値: (更新後のアクション, 予算配分表, 除外する商品管理番号のリスト)"""
    from .strategy import ITEM_CPC, ITEM_EXCLUDE, KW_DELETE, KW_NEW, KW_UPDATE

    act = plan_actions.copy()
    days = period_days or 30
    cap = monthly_budget * 0.9
    suit_idx = suit.set_index("商品管理番号")
    lines: List[dict] = []

    def line(prio, kind, iid, kw, cpc, monthly, why):
        name = universe.set_index("商品管理番号")["商品名"].get(iid, "")
        lines.append({"優先": prio, "区分": kind, "商品管理番号": iid, "商品名": name, "キーワード": kw,
                      "CPC": cpc, "月の見込み費用": round(monthly), "根拠": why})

    # ① ③ 既存のキーワード配信（削除するものを除く）
    deleted = set(zip(act.loc[(act["施策"] == KW_DELETE) & act["実行"], "商品管理番号"],
                      act.loc[(act["施策"] == KW_DELETE) & act["実行"], "キーワード"]))
    upd = {(r["商品管理番号"], r["キーワード"]): r["変更後CPC"] for _, r in act[act["施策"] == KW_UPDATE].iterrows()}
    if kw_stats is not None and not kw_stats.empty:
        for _, k in kw_stats.iterrows():
            key = (k["商品管理番号"], k["キーワード"])
            if key in deleted:
                continue
            base = k["CPC実績"] if pd.notna(k.get("CPC実績")) and k["CPC実績"] > 0 else None
            new = upd.get(key, base)
            per_day = k["実績額"] / days
            monthly = per_day * 30 * ((new / base) if base and new else 1.0)
            est = k.get("見込みROAS(%)")
            tgt = k.get("目標ROAS(%)")
            has = est is not None and not pd.isna(est) and tgt
            if has and est >= tgt:
                prio, why = 1, f"目標達成の配信（見込みROAS {est:.0f}%）"
            elif has and est >= tgt * 0.5:
                prio, why = 3, f"改善中の配信（見込みROAS {est:.0f}%。CPCを調整して継続）"
            elif has:
                prio, why = 5, f"大幅な赤字（見込みROAS {est:.0f}%）。テスト配信より後回し"
            else:
                prio, why = 3, "実績が少ないため継続して判定"
            line(prio, "既存キーワード", key[0], key[1], new, monthly, why)

    # 既存の商品CPC配信（商品別レポートがある場合）
    for _, r in act[(act["施策"] == ITEM_CPC) & (act["コントロール"] == "u")].iterrows():
        it = items.set_index("商品管理番号").loc[r["商品管理番号"]] if not items.empty and r["商品管理番号"] in set(items["商品管理番号"]) else None
        if it is None:
            continue
        nk_sp = it.get("KW外広告費")
        nk_sp = float(nk_sp) if nk_sp is not None and not pd.isna(nk_sp) else float(it["実績額"])
        monthly = nk_sp / days * 30 * (r["変更後CPC"] / r["変更前CPC"] if r["変更前CPC"] else 1)
        line(3, "既存の商品CPC", r["商品管理番号"], "", r["変更後CPC"], monthly, "キーワード以外の配信")

    # ② 新規キーワード（判定に必要なクリック数ぶんのテスト費用）
    for _, r in act[act["施策"] == KW_NEW].iterrows():
        sc = suit_idx["広告適性"].get(r["商品管理番号"], 0)
        line(2, "新規キーワード", r["商品管理番号"], r["キーワード"], r["変更後CPC"], s.min_clicks * r["変更後CPC"],
             f"テスト{s.min_clicks}クリック分（広告適性{sc}点）")

    # ④ 実績づくりのテスト配信（商品CPC n）
    for _, r in act[(act["施策"] == ITEM_CPC) & (act["コントロール"] == "n")].iterrows():
        sc = suit_idx["広告適性"].get(r["商品管理番号"], 0)
        line(4, "テスト配信", r["商品管理番号"], "", r["変更後CPC"], s.min_clicks * r["変更後CPC"],
             f"CVR測定のため{s.min_clicks}クリック分（広告適性{sc}点）")

    bt = pd.DataFrame(lines, columns=["優先", "区分", "商品管理番号", "商品名", "キーワード", "CPC", "月の見込み費用", "根拠"])
    if bt.empty:
        return act, bt.assign(予算=[]), []
    bt["広告適性"] = bt["商品管理番号"].map(suit_idx["広告適性"]).fillna(0)
    bt["適性判定"] = bt["商品管理番号"].map(suit_idx["適性判定"]).fillna(UNSUITABLE)
    bt = bt.sort_values(["優先", "広告適性"], ascending=[True, False], kind="stable").reset_index(drop=True)

    funded, total = [], 0.0
    for i, r in bt.iterrows():
        ok = True
        why = ""
        if r["区分"] in ("テスト配信", "新規キーワード") and r["適性判定"] == UNSUITABLE:
            ok, why = False, "広告に適さない"
        elif r["優先"] >= 2 and total + r["月の見込み費用"] > cap:
            ok, why = False, "予算の上限"
        if ok:
            total += r["月の見込み費用"]
        funded.append(("配分" if ok else f"見送り（{why}）"))
    bt["予算"] = funded
    bt["累計"] = bt["月の見込み費用"].where(bt["予算"] == "配分", 0).cumsum()

    # 予算の付かなかった既存キーワードは、今月は下限CPCまで下げる（削除はせず、配信をほぼ止める）
    off_exist = set(zip(bt.loc[(bt["区分"] == "既存キーワード") & (bt["予算"] != "配分"), "商品管理番号"],
                        bt.loc[(bt["区分"] == "既存キーワード") & (bt["予算"] != "配分"), "キーワード"]))
    # 予算の付かなかった新規キーワード・テスト配信は実行しない
    off_kw = set(zip(bt.loc[(bt["区分"] == "新規キーワード") & (bt["予算"] != "配分"), "商品管理番号"],
                     bt.loc[(bt["区分"] == "新規キーワード") & (bt["予算"] != "配分"), "キーワード"]))
    on_test = set(bt.loc[(bt["区分"] == "テスト配信") & (bt["予算"] == "配分"), "商品管理番号"])
    for i, r in act.iterrows():
        if r["施策"] == KW_NEW and (r["商品管理番号"], r["キーワード"]) in off_kw:
            act.at[i, "実行"] = False
            act.at[i, "根拠"] += "【予算外のため今月は見送り】"
        if r["施策"] == ITEM_CPC and r["コントロール"] == "n":
            act.at[i, "実行"] = r["商品管理番号"] in on_test
            if r["商品管理番号"] not in on_test:
                act.at[i, "根拠"] += "【予算外または広告に適さないため今月は見送り】"

    for key in off_exist:
        m = (act["施策"] == KW_UPDATE) & (act["商品管理番号"] == key[0]) & (act["キーワード"] == key[1])
        if m.any():
            act.loc[m, "変更後CPC"] = s.min_kw_cpc
            act.loc[m, "根拠"] = act.loc[m, "根拠"] + f"【予算外のため今月は下限{s.min_kw_cpc}円に】"
        elif not ((act["施策"] == KW_DELETE) & (act["商品管理番号"] == key[0]) & (act["キーワード"] == key[1])).any():
            base = kw_stats[(kw_stats["商品管理番号"] == key[0]) & (kw_stats["キーワード"] == key[1])]
            cur = base["CPC実績"].iloc[0] if not base.empty else None
            act = pd.concat([act, pd.DataFrame([{
                "実行": True, "優先度": 2, "施策": KW_UPDATE, "商品管理番号": key[0],
                "商品名": universe.set_index("商品管理番号")["商品名"].get(key[0], ""), "キーワード": key[1],
                "変更前CPC": None if cur is None or pd.isna(cur) else int(round(cur)), "変更後CPC": s.min_kw_cpc,
                "コントロール": "u", "根拠": f"予算の優先度が低いため、今月は下限{s.min_kw_cpc}円に下げる", "影響": "予算の保護"}])],
                ignore_index=True)

    # 除外: 予算の付いた配信が1つも無い商品（＝キャンペーンCPCで費用が流れるのを止める）
    funded_items = set(bt.loc[bt["予算"] == "配分", "商品管理番号"])
    exclude = [iid for iid in universe["商品管理番号"] if iid not in funded_items]
    existing_ex = set(act.loc[act["施策"] == ITEM_EXCLUDE, "商品管理番号"])
    new_rows = []
    for iid in exclude:
        sc = suit_idx.loc[iid] if iid in suit_idx.index else None
        why = (f"広告適性{int(sc['広告適性'])}点（{sc['適性判定']}）。{sc['適性の理由']}" if sc is not None else "")
        if iid in existing_ex:
            act.loc[(act["施策"] == ITEM_EXCLUDE) & (act["商品管理番号"] == iid), "実行"] = True
            continue
        new_rows.append({"実行": True, "優先度": 3, "施策": ITEM_EXCLUDE, "商品管理番号": iid,
                         "商品名": universe.set_index("商品管理番号")["商品名"].get(iid, ""), "キーワード": "",
                         "変更前CPC": None, "変更後CPC": None, "コントロール": "n",
                         "根拠": f"今月は予算を配分しないため除外（キャンペーンCPCでの配信を止める）。{why}",
                         "影響": "予算の保護"})
    act.loc[act["施策"] == ITEM_EXCLUDE, "コントロール"] = "n"
    if new_rows:
        act = pd.concat([act, pd.DataFrame(new_rows)], ignore_index=True)
    # 除外する商品のキーワード変更・追加・商品CPCは実行しない（除外が優先）
    ex = set(exclude)
    mask = act["商品管理番号"].isin(ex) & act["施策"].isin([KW_UPDATE, KW_NEW, ITEM_CPC])
    act.loc[mask, "実行"] = False
    return act, bt, exclude
