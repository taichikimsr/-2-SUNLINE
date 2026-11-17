"""RPP自動運用エンジン

レポートを読み込んだら、次の順で「何をするか（アクションプラン）」を自動で決める。

1. 商品診断
   - 粗利率から損益分岐ROASと目標ROASを決める
   - 1クリックあたり売上（クリック数の少ない商品は店舗平均に寄せて補正）から
     「目標ROASを守れる上限CPC」を計算する
   - キーワードCPCの下限（40円）で目標ROASを満たすのに必要なCVRを計算し、
     CPC調整では黒字化できない商品（構造的赤字）を見分ける
   - 商品別とキーワード別の両方がある場合は、キーワード経由以外のクリック
     （商品CPCで配信されている部分）の成績を分けて評価する
2. 登録済みキーワードの入札
   - 目標ROASに合わせて上げ下げ（1回の変更幅は上限あり）
   - 粗利1件分以上を使って受注ゼロのキーワードは削除
   - 成果の良いキーワードは楽天の目安CPCまで早めに引き上げて露出を取りに行く
   - 前回の運用記録があれば、下げすぎて露出が落ちたキーワードを戻す
3. 新規キーワード（攻め）
   - 検索キーワードデータ・他商品で売れている語・商品名から候補を作り、
     採算が合う商品にだけ、テスト用CPCで追加する
4. 商品CPC（守り）
   - キーワード以外の広く浅い配信は、商品CPCを目標ROASに合わせて抑える
   - 下限でも大幅に赤字の商品は除外候補にする
5. ページ改善
   - 構造的赤字の商品には、必要なCVRと確認項目を提示する

数値はすべてレポート期間の実績にもとづく。検索数など、レポートにない情報は推測していない。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .keywords import (
    category_terms, normalize_keyword, relevance, script_aliases, title_candidates, token_document_frequency,
)

# アクション種別
KW_UPDATE = "キーワードCPC変更"
KW_NEW = "キーワード追加"
KW_DELETE = "キーワード削除"
ITEM_CPC = "商品CPC変更"
ITEM_EXCLUDE = "除外候補"
PAGE_FIX = "ページ改善"

# 商品の分類
CLS_GROW = "拡大"
CLS_KEEP = "維持"
CLS_TUNE = "調整"
CLS_STRUCT = "構造的赤字"
CLS_DATA = "データ不足"
CLS_NEW = "未配信（推定）"

# 投資区分（全商品のポートフォリオ）
INV_ATTACK = "攻め（キーワード投資）"
INV_DEFEND = "守り（商品CPCのみ）"
INV_STOP = "停止候補"
INV_TEST = "テスト（実績づくり）"

MODES = {
    # 目標ROAS ＝ 損益分岐ROAS × 係数
    "利益重視": {"mult": 1.3, "new_kw_unprofitable": False, "new_kw_per_item": 3},
    "バランス": {"mult": 1.1, "new_kw_unprofitable": False, "new_kw_per_item": 5},
    "売上拡大": {"mult": 0.9, "new_kw_unprofitable": True, "new_kw_per_item": 8},
}


@dataclass
class Strategy:
    default_margin: float = 30.0       # 粗利率(%)：商品マスタに無い商品に使う
    mode: str = "バランス"
    existing_weight: float = 1.0       # 既存顧客の売上の評価（0.5なら半分として扱う）
    event_boost: float = 0.0           # セール期間中の入札上乗せ（0.2 = +20%）
    min_item_cpc: int = 20
    min_kw_cpc: int = 40
    max_cpc: int = 300
    max_change_rate: float = 0.25      # 1回あたりの最大変更幅
    min_clicks: int = 30               # 判定に必要なクリック数
    prior_clicks: float = 30.0         # 平滑化の強さ
    max_kw_per_item: int = 20          # 1商品あたりのキーワード上限
    new_kw_per_item: Optional[int] = None  # 1回で追加する上限（None ならモードの既定値）
    new_kw_max_cpc: int = 80           # 新規キーワードのテスト用CPCの上限
    delete_ratio: float = 1.0          # 粗利1件分 × この倍率 を使って受注0なら削除
    keep_band: float = 0.05
    assumed_cvr: float = 1.0           # 実績の無い商品に使う想定CVR(%)（店舗の実績があれば実績と混ぜる）
    monthly_budget: float = 0.0        # 月予算（円）。0なら予算配分・除外は行わない

    @property
    def mult(self) -> float:
        return MODES.get(self.mode, MODES["バランス"])["mult"]

    @property
    def per_item_new(self) -> int:
        if self.new_kw_per_item is not None:
            return self.new_kw_per_item
        return MODES.get(self.mode, MODES["バランス"])["new_kw_per_item"]

    @property
    def allow_unprofitable_new(self) -> bool:
        return MODES.get(self.mode, MODES["バランス"])["new_kw_unprofitable"]


@dataclass
class Plan:
    items: pd.DataFrame
    keywords: pd.DataFrame
    actions: pd.DataFrame
    summary: Dict[str, float]
    feedback: pd.DataFrame = field(default_factory=pd.DataFrame)
    notes: List[str] = field(default_factory=list)
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)
    portfolio: pd.DataFrame = field(default_factory=pd.DataFrame)
    budget: pd.DataFrame = field(default_factory=pd.DataFrame)


# ------------------------------------------------------------------ 共通計算
def _value(df: pd.DataFrame, s: Strategy) -> pd.Series:
    """評価に使う売上。既存顧客の重みが1未満で、新規/既存の内訳があれば重み付けする。"""
    total = df["売上金額"].fillna(0)
    if s.existing_weight >= 1 or "新規売上" not in df or df["新規売上"].isna().all():
        return total
    new = df["新規売上"].fillna(0)
    existing = df["既存売上"].fillna((total - new).clip(lower=0))
    return new + existing * s.existing_weight


def _smooth(value: float, clicks: float, prior_vpc: float, k: float) -> float:
    return (value + prior_vpc * k) / (clicks + k) if clicks + k > 0 else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _fmt(v: float) -> str:
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:,.0f}"


# ------------------------------------------------------------------ 商品診断
def diagnose_items(
    item_rep: Optional[pd.DataFrame],
    kw_rep: Optional[pd.DataFrame],
    master: Optional[pd.DataFrame],
    s: Strategy,
) -> pd.DataFrame:
    if item_rep is None or item_rep.empty:
        if kw_rep is None or kw_rep.empty:
            raise ValueError("商品別またはキーワード別のレポートが必要です")
        # 商品別が無い場合はキーワード別を商品単位に合算して代用する
        agg_cols = {c: "sum" for c in ("クリック数", "実績額", "売上金額", "売上件数", "新規売上", "既存売上")
                    if c in kw_rep}
        item_rep = kw_rep.groupby("商品管理番号", as_index=False).agg({**agg_cols, "商品名": "first"})
        item_rep["現在CPC"] = np.nan
        item_rep["目安CPC"] = np.nan
        item_rep["CPC実績"] = item_rep["実績額"] / item_rep["クリック数"].replace(0, np.nan)
        item_rep["_source"] = "キーワード別から合算"
    else:
        item_rep = item_rep.copy()
        item_rep["_source"] = "商品別"

    df = item_rep.copy()
    df["評価売上"] = _value(df, s)

    # 商品マスタ（粗利率・目標ROAS・販売価格）
    df["粗利率"] = s.default_margin
    df["個別目標ROAS"] = np.nan
    if master is not None and not master.empty:
        m = master.copy()
        m["商品管理番号"] = m["商品管理番号"].astype(str).str.strip()
        m = m.drop_duplicates("商品管理番号", keep="last").set_index("商品管理番号")
        if "粗利率" in m:
            df["粗利率"] = df["商品管理番号"].map(m["粗利率"]).fillna(s.default_margin)
        if "目標ROAS" in m:
            df["個別目標ROAS"] = df["商品管理番号"].map(m["目標ROAS"])

    total_clicks = df["クリック数"].sum()
    store_vpc = df["評価売上"].sum() / total_clicks if total_clicks > 0 else 0.0
    total_orders = df["売上件数"].sum()
    store_aov = df["売上金額"].sum() / total_orders if total_orders > 0 else np.nan

    # キーワード経由のクリックを商品ごとに合算（キーワード以外＝商品CPCでの配信を分けて評価）
    kw_sum = None
    if kw_rep is not None and not kw_rep.empty and (df["_source"] == "商品別").all():
        tmp = kw_rep.copy()
        tmp["評価売上"] = _value(tmp, s)
        kw_sum = tmp.groupby("商品管理番号")[["クリック数", "実績額", "評価売上", "売上件数"]].sum()

    rows = []
    for _, r in df.iterrows():
        c, sp, v, o = (float(r["クリック数"]), float(r["実績額"]), float(r["評価売上"]), float(r["売上件数"]))
        margin = float(r["粗利率"])
        breakeven_roas = 100.0 / (margin / 100.0) if margin > 0 else np.inf
        target = float(r["個別目標ROAS"]) if pd.notna(r["個別目標ROAS"]) and r["個別目標ROAS"] > 0 \
            else breakeven_roas * s.mult
        aov = float(r["売上金額"]) / o if o > 0 else store_aov
        vpc_s = _smooth(v, c, store_vpc, s.prior_clicks)
        target_cpc = vpc_s / (target / 100.0)
        roas = v / sp * 100 if sp > 0 else np.nan
        est_roas = vpc_s / (sp / c) * 100 if c > 0 and sp > 0 else np.nan
        cvr = o / c * 100 if c > 0 else np.nan
        need_cvr = (s.min_kw_cpc * target / 100.0) / aov * 100 if aov and not math.isnan(aov) else np.nan
        profit = float(r["売上金額"]) * margin / 100.0 - sp

        if c < s.min_clicks:
            cls = CLS_DATA
        elif target_cpc < s.min_kw_cpc * 0.75 and (math.isnan(est_roas) or est_roas < target * 0.8):
            # 採算の合うCPCが下限の3/4未満＝CVRを1.3倍以上にしないとキーワード広告で黒字にならない
            cls = CLS_STRUCT
        elif not math.isnan(est_roas) and est_roas >= target * 1.2:
            cls = CLS_GROW
        elif not math.isnan(est_roas) and est_roas >= target * 0.9:
            cls = CLS_KEEP
        else:
            cls = CLS_TUNE

        # キーワード以外の配信
        nk = {"クリック": np.nan, "広告費": np.nan, "売上": np.nan, "ROAS": np.nan}
        if kw_sum is not None and r["商品管理番号"] in kw_sum.index:
            ks = kw_sum.loc[r["商品管理番号"]]
            nk_c = max(c - ks["クリック数"], 0)
            nk_sp = max(sp - ks["実績額"], 0)
            nk_v = max(v - ks["評価売上"], 0)
            nk = {"クリック": nk_c, "広告費": nk_sp, "売上": nk_v,
                  "ROAS": nk_v / nk_sp * 100 if nk_sp > 0 else np.nan}
        elif kw_sum is not None:
            nk = {"クリック": c, "広告費": sp, "売上": v, "ROAS": roas}

        rows.append({
            "目標ROAS(%)": round(target, 0),
            "損益分岐ROAS(%)": round(breakeven_roas, 0),
            "ROAS(%)": None if math.isnan(roas) else round(roas, 1),
            "見込みROAS(%)": None if math.isnan(est_roas) else round(est_roas, 1),
            "CVR(%)": None if math.isnan(cvr) else round(cvr, 2),
            "必要CVR(%)": None if math.isnan(need_cvr) else round(need_cvr, 2),
            "客単価": None if math.isnan(aov) else round(aov, 0),
            "1クリック売上": round(vpc_s, 1),
            "目標CPC": round(target_cpc, 1),
            "粗利-広告費": round(profit, 0),
            "KW外クリック": nk["クリック"],
            "KW外広告費": nk["広告費"],
            "KW外売上": nk["売上"],
            "KW外ROAS(%)": None if nk["ROAS"] is None or (isinstance(nk["ROAS"], float) and math.isnan(nk["ROAS"]))
            else round(nk["ROAS"], 1),
            "分類": cls,
        })
    out = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    out.attrs["store_vpc"] = store_vpc
    out.attrs["store_aov"] = store_aov
    return out


# ------------------------------------------------------------------ 前回の運用記録
def _history_index(history: Optional[pd.DataFrame]) -> Dict[tuple, pd.Series]:
    if history is None or history.empty:
        return {}
    h = history.copy()
    for c in ("クリック数", "集計日数", "変更前CPC", "変更後CPC"):
        if c in h:
            h[c] = pd.to_numeric(h[c], errors="coerce")
    idx = {}
    for _, r in h.iterrows():
        key = (str(r.get("商品管理番号", "")).strip(), normalize_keyword(r.get("キーワード", "") or ""))
        idx[key] = r
    return idx


# ------------------------------------------------------------------ メイン
def build_plan(
    item_rep: Optional[pd.DataFrame],
    kw_rep: Optional[pd.DataFrame],
    s: Strategy,
    master: Optional[pd.DataFrame] = None,
    search_kw: Optional[pd.DataFrame] = None,
    history: Optional[pd.DataFrame] = None,
    period_days: Optional[int] = None,
    catalog: Optional[pd.DataFrame] = None,
) -> Plan:
    notes: List[str] = []
    if (item_rep is None or item_rep.empty) and (kw_rep is None or kw_rep.empty):
        if catalog is None or catalog.empty:
            raise ValueError("RPPのレポートか商品一覧が必要です")
        items = _empty_items()
        notes.append("RPPのレポートが無いため、全商品を想定CVRと価格から推定して設計しています。"
                     "レポートを入れると実績にもとづく判断に切り替わります。")
    else:
        items = diagnose_items(item_rep, kw_rep, master, s)
    if "_source" in items and (items["_source"] == "キーワード別から合算").any():
        notes.append("商品別レポートが無いため、キーワード別レポートを商品ごとに合算して診断しています。"
                     "商品CPCの調整と、キーワード以外の配信の評価には商品別レポートが必要です。")
    item_map = items.set_index("商品管理番号")
    hist = _history_index(history)
    actions: List[dict] = []
    feedback_rows: List[dict] = []

    def add(kind, item_id, kw, cur, new, reason, ctrl="", execute=True, priority=2, impact=""):
        name = item_map.loc[item_id, "商品名"] if item_id in item_map.index else ""
        actions.append({
            "実行": execute, "優先度": priority, "施策": kind, "商品管理番号": item_id, "商品名": name,
            "キーワード": kw, "変更前CPC": cur, "変更後CPC": new, "コントロール": ctrl,
            "根拠": reason, "影響": impact,
        })

    # ---------------- 登録済みキーワード
    kw_out = pd.DataFrame()
    registered: Dict[str, set] = {}
    if kw_rep is not None and not kw_rep.empty:
        kw = kw_rep.copy()
        kw["評価売上"] = _value(kw, s)
        kw_rows = []
        for _, r in kw.iterrows():
            iid = r["商品管理番号"]
            registered.setdefault(iid, set()).add(normalize_keyword(r["キーワード"]))
            it = item_map.loc[iid] if iid in item_map.index else None
            target = float(it["目標ROAS(%)"]) if it is not None else 100 / (s.default_margin / 100) * s.mult
            margin = float(it["粗利率"]) if it is not None else s.default_margin
            aov = it["客単価"] if it is not None and pd.notna(it["客単価"]) else items.attrs.get("store_aov")
            prior = float(it["1クリック売上"]) if it is not None else items.attrs.get("store_vpc", 0)
            c, sp, v, o = float(r["クリック数"]), float(r["実績額"]), float(r["評価売上"]), float(r["売上件数"])
            vpc_s = _smooth(v, c, prior, s.prior_clicks)
            tgt = vpc_s / (target / 100.0)
            base = r["現在CPC"] if pd.notna(r["現在CPC"]) and r["現在CPC"] > 0 else r["CPC実績"]
            base = float(base) if pd.notna(base) and base > 0 else _clamp(tgt, s.min_kw_cpc, s.max_cpc)
            guide = float(r["目安CPC"]) if pd.notna(r.get("目安CPC")) else np.nan
            roas = v / sp * 100 if sp > 0 else np.nan
            est = vpc_s / (sp / c) * 100 if c > 0 and sp > 0 else np.nan
            allowance = (aov * margin / 100.0 * s.delete_ratio) if aov and not math.isnan(aov) else np.inf
            lo, hi = base * (1 - s.max_change_rate), base * (1 + s.max_change_rate)
            kind, new, reason, execute, prio = None, base, "", True, 3

            h = hist.get((iid, normalize_keyword(r["キーワード"])))
            if o == 0 and c >= s.min_clicks and sp >= allowance:
                kind, new, prio = KW_DELETE, base, 1
                reason = (f"{c:.0f}クリック・広告費{_fmt(sp)}円で受注0件。"
                          f"粗利1件分（約{_fmt(allowance)}円）を超えて使っているため削除")
            elif o == 0 and sp >= allowance:
                kind, new, prio = KW_UPDATE, max(lo, s.min_kw_cpc), 1
                reason = f"受注0件で粗利1件分（約{_fmt(allowance)}円）を超過。クリックがまだ少ないため削除せず引き下げ"
            elif c < s.min_clicks:
                kind = None
                reason = f"クリック{c:.0f}回（{s.min_clicks}回未満）のため判定保留"
            else:
                if not math.isnan(est) and est < target * 0.5:
                    # 目標の半分未満の大幅赤字は、通常より大きく（最大-50%）下げる
                    lo = base * 0.5
                new = _clamp(tgt, lo, hi)
                if not math.isnan(est) and est >= target * 1.2 and not math.isnan(guide) and guide > new and tgt > new:
                    # 成果が良いのに楽天の目安CPCより低い → 露出を取りに早めに引き上げ
                    new = min(tgt, guide, base * 1.5)
                    reason = (f"見込みROAS {est:.0f}%（目標{target:.0f}%）と好調。目安CPC {guide:.0f}円まで引き上げて露出を拡大")
                elif abs(tgt - base) / base <= s.keep_band:
                    new = base
                    reason = f"見込みROAS {est:.0f}%で目標{target:.0f}%付近"
                elif new > base:
                    reason = f"見込みROAS {est:.0f}%が目標{target:.0f}%を上回る。CPCを上げて表示機会を増やす"
                else:
                    reason = f"見込みROAS {est:.0f}%が目標{target:.0f}%を下回る（実績{_fmt(roas)}%）"
                new = _clamp(new, s.min_kw_cpc, s.max_cpc)
                kind = KW_UPDATE
                if new <= s.min_kw_cpc and (math.isnan(est) or est < target * 0.5):
                    # 下限でも採算が合わない
                    if o == 0:
                        kind, prio = KW_DELETE, 1
                        reason += f"。下限{s.min_kw_cpc}円でも採算が合わず受注0件のため削除"
                    else:
                        kind, prio, execute = KW_DELETE, 2, False
                        reason += (f"。下限{s.min_kw_cpc}円でも目標の半分未満。受注はあるため削除は手動で判断"
                                   "（ランキング維持など目的があれば残す）")
                # 前回の運用記録：下げた結果、露出が大きく落ちていれば一部戻す
                if h is not None and str(h.get("施策")) == KW_UPDATE and kind == KW_UPDATE \
                        and pd.notna(h.get("変更前CPC")) and pd.notna(h.get("変更後CPC")) \
                        and h["変更後CPC"] < h["変更前CPC"] and period_days and pd.notna(h.get("集計日数")):
                    prev_cpd = h["クリック数"] / h["集計日数"] if h["集計日数"] else np.nan
                    now_cpd = c / period_days
                    if prev_cpd and now_cpd < prev_cpd * 0.5 and not math.isnan(est) and est >= target:
                        new = min(s.max_cpc, max(new, base * 1.1))
                        reason = (f"前回の引き下げ後、1日あたりクリックが{prev_cpd:.1f}→{now_cpd:.1f}に半減。"
                                  f"見込みROAS {est:.0f}%は目標以上のため一部戻す")

            # セール期間の上乗せ（成果の出ているキーワードのみ）
            if kind == KW_UPDATE and s.event_boost > 0 and not math.isnan(est) and est >= target:
                new = min(s.max_cpc, new * (1 + s.event_boost))
                reason += f"。セール期間のため+{s.event_boost:.0%}（終了後の運用で通常値に戻ります）"

            new_i = int(round(new))
            if kind == KW_UPDATE and new_i == int(round(base)):
                kind = None
            if kind:
                ctrl = "d" if kind == KW_DELETE else "u"
                add(kind, iid, r["キーワード"], int(round(base)), new_i if kind != KW_DELETE else None,
                    reason, ctrl, execute, prio,
                    impact=f"期間中の広告費{_fmt(sp)}円・売上{_fmt(r['売上金額'])}円")
            kw_rows.append({**r.to_dict(), "目標ROAS(%)": round(target, 0), "見込みROAS(%)": None if math.isnan(est)
                            else round(est, 1), "目標CPC": round(tgt, 1), "判定": kind or "据え置き", "根拠": reason})
            if h is not None and pd.notna(h.get("変更後CPC")):
                feedback_rows.append({
                    "商品管理番号": iid, "キーワード": r["キーワード"], "前回の施策": h.get("施策"),
                    "前回CPC": h.get("変更前CPC"), "変更後CPC": h.get("変更後CPC"),
                    "前回ROAS(%)": h.get("ROAS(%)"), "今回ROAS(%)": None if math.isnan(roas) else round(roas, 1),
                    "前回クリック/日": round(h["クリック数"] / h["集計日数"], 2) if pd.notna(h.get("集計日数")) and h["集計日数"] else None,
                    "今回クリック/日": round(c / period_days, 2) if period_days else None,
                })
        kw_out = pd.DataFrame(kw_rows)

    # ---------------- 商品CPC・除外・ページ改善
    has_item_report = not items.empty and (items["_source"] == "商品別").all()
    has_item_report_flag = has_item_report
    for _, it in items.iterrows():
        iid = it["商品管理番号"]
        cls = it["分類"]
        target = float(it["目標ROAS(%)"])
        if cls == CLS_STRUCT and pd.notna(it["必要CVR(%)"]):
            add(PAGE_FIX, iid, "", None, None,
                f"CVR {it['CVR(%)']}% → {it['必要CVR(%)']}%以上が必要（キーワード下限CPC{s.min_kw_cpc}円・目標ROAS{target:.0f}%の場合）。"
                "広告の調整だけでは黒字化できないため、価格・送料・ポイント・レビュー件数・メイン画像・"
                "商品名の先頭30文字・在庫/納期表示を見直してください",
                execute=False, priority=1, impact=f"期間中の粗利-広告費 {_fmt(it['粗利-広告費'])}円")
        if not has_item_report:
            continue
        base = it["現在CPC"] if pd.notna(it["現在CPC"]) and it["現在CPC"] > 0 else it["CPC実績"]
        if pd.isna(base) or base <= 0:
            continue
        base = float(base)
        nk_c = it["KW外クリック"] if pd.notna(it["KW外クリック"]) else it["クリック数"]
        nk_sp = it["KW外広告費"] if pd.notna(it["KW外広告費"]) else it["実績額"]
        nk_v = it["KW外売上"] if pd.notna(it["KW外売上"]) else it["評価売上"]
        vpc_nk = _smooth(float(nk_v), float(nk_c), float(it["1クリック売上"]), s.prior_clicks)
        tgt = vpc_nk / (target / 100.0)
        if nk_c < s.min_clicks:
            continue
        lo, hi = base * (1 - s.max_change_rate), base * (1 + s.max_change_rate)
        new = _clamp(_clamp(tgt, lo, hi), s.min_item_cpc, s.max_cpc)
        nk_roas = nk_v / nk_sp * 100 if nk_sp > 0 else np.nan
        if abs(tgt - base) / base > s.keep_band and int(round(new)) != int(round(base)):
            direction = "上げて配信を広げる" if new > base else "抑える"
            add(ITEM_CPC, iid, "", int(round(base)), int(round(new)),
                f"キーワード以外の配信（{nk_c:.0f}クリック）のROAS {_fmt(nk_roas)}%・目標{target:.0f}%。商品CPCを{direction}",
                ctrl="u", priority=2, impact=f"KW外の広告費{_fmt(nk_sp)}円")
        if new <= s.min_item_cpc and (math.isnan(nk_roas) or nk_roas < target * 0.5):
            add(ITEM_EXCLUDE, iid, "", None, None,
                f"商品CPCを下限{s.min_item_cpc}円にしても目標ROASの半分未満（KW外ROAS {_fmt(nk_roas)}%）。"
                "RPPの除外商品への登録を検討（キーワードでの配信だけ残す選択肢もあります）",
                execute=False, priority=2, impact=f"KW外の広告費{_fmt(nk_sp)}円")

    # ---------------- 新規キーワード
    universe = build_universe(items, catalog, master, s)
    if catalog is not None and not catalog.empty:
        reported = set(items["商品管理番号"]) if not items.empty else set()
        for _, u in universe[universe["分類"] == CLS_NEW].iterrows():
            if u["商品管理番号"] in reported or pd.isna(u["目標CPC"]):
                continue
            cpc = int(round(_clamp(u["目標CPC"], s.min_item_cpc, max(s.min_item_cpc, s.min_kw_cpc - 1))))
            add(ITEM_CPC, u["商品管理番号"], "", None, cpc,
                f"RPPの実績なし。推定の採算CPC {u['目標CPC']:.0f}円（価格{_fmt(u['客単価'])}円×想定CVR・{u['価格の出所']}）。"
                f"商品CPC {cpc}円で少額配信して実際のCVRを測る",
                ctrl="n", execute=has_item_report_flag, priority=3, impact="テスト配信")
    if catalog is not None and not catalog.empty:
        no_price = universe["価格の出所"].eq("店舗平均（推定）").sum()
        if no_price:
            notes.append(f"{no_price}商品は販売価格が分からないため、店舗の平均客単価で推定しています。"
                         "販売価格の入った商品一覧（RMSの商品CSVなど）を入れると精度が上がります。")
    new_kw, cand_df = _new_keywords(universe, kw_rep, search_kw, registered, s)
    for row in new_kw:
        add(KW_NEW, row["商品管理番号"], row["キーワード"], None, row["CPC"], row["根拠"], "n",
            True, 2, impact=row["影響"])

    act = pd.DataFrame(actions, columns=["実行", "優先度", "施策", "商品管理番号", "商品名", "キーワード",
                                          "変更前CPC", "変更後CPC", "コントロール", "根拠", "影響"])
    act = act.sort_values(["優先度", "施策", "商品管理番号"], kind="stable").reset_index(drop=True)

    spend = items["実績額"].sum()
    sales = items["売上金額"].sum()
    del_spend = 0.0
    if not kw_out.empty:
        dmask = (act["施策"] == KW_DELETE) & act["実行"]
        del_keys = set(zip(act.loc[dmask, "商品管理番号"], act.loc[dmask, "キーワード"]))
        del_spend = kw_out[[(a, b) in del_keys for a, b in zip(kw_out["商品管理番号"], kw_out["キーワード"])]]["実績額"].sum()
    summary = {
        "集計日数": period_days,
        "広告費": spend,
        "売上": sales,
        "ROAS(%)": sales / spend * 100 if spend > 0 else np.nan,
        "粗利-広告費": float(items["粗利-広告費"].sum()) if "粗利-広告費" in items else 0.0,
        "削除対象の広告費": del_spend,
        "構造的赤字の商品数": int((items["分類"] == CLS_STRUCT).sum()) if "分類" in items else 0,
        "アクション数": int(act["実行"].sum()),
        "新規キーワード数": int((act["施策"] == KW_NEW).sum()),
    }
    if not cand_df.empty and new_kw == []:
        waiting = cand_df["判定"].str.startswith("待機").sum()
        notes.append(f"新規キーワード候補を{len(cand_df)}件作成しましたが、想定1クリック売上から計算したCPCがキーワード下限"
                     f"（{s.min_kw_cpc}円）に届かず、今追加すると赤字になるため投入していません"
                     + (f"（うち{waiting}件は実績づくり後に投入する「待機」）" if waiting else "")
                     + "。「キーワード設計」タブで商品ごとの候補と理由を確認できます。売上拡大モードでは下限CPCで追加します。")
    # ---------------- 広告適性と月予算
    from .budget import allocate_budget, suitability
    brand_terms = _brand_terms(universe, registered)
    suit = suitability(universe, s, search_kw=search_kw, brand_terms=brand_terms)
    budget_table = pd.DataFrame()
    if s.monthly_budget and s.monthly_budget > 0:
        act, budget_table, excluded = allocate_budget(act, universe, suit, kw_out, items, s, s.monthly_budget,
                                                      period_days)
        act = act.sort_values(["優先度", "施策", "商品管理番号"], kind="stable").reset_index(drop=True)
        funded = budget_table.loc[budget_table["予算"] == "配分", "月の見込み費用"].sum() if not budget_table.empty else 0
        summary["月予算"] = s.monthly_budget
        summary["配分した見込み費用"] = funded
        summary["除外する商品数"] = len(excluded)
        summary["アクション数"] = int(act["実行"].sum())
    portfolio = _portfolio(universe, act, registered, s)
    if not portfolio.empty:
        portfolio = portfolio.merge(suit, on="商品管理番号", how="left")
        portfolio = portfolio.sort_values("広告適性", ascending=False, kind="stable").reset_index(drop=True)
    summary["全商品数"] = len(portfolio)
    for k in (INV_ATTACK, INV_TEST, INV_DEFEND, INV_STOP):
        summary[k] = int((portfolio["投資区分"] == k).sum()) if not portfolio.empty else 0
    return Plan(items=items, keywords=kw_out, actions=act,
                summary={k: (float(v) if isinstance(v, (int, float, np.number)) and v is not None else v)
                         for k, v in summary.items()},
                feedback=pd.DataFrame(feedback_rows), notes=notes, candidates=cand_df, portfolio=portfolio,
                budget=budget_table)


def _new_keywords(items, kw_rep, search_kw, registered, s: Strategy):
    names = dict(zip(items["商品管理番号"], items["商品名"].astype(str)))
    vpc_map = dict(zip(items["商品管理番号"], items["1クリック売上"].astype(float)))
    cat_map = dict(zip(items["商品管理番号"], items["カテゴリ"].astype(str))) if "カテゴリ" in items else {}
    doc_freq = token_document_frequency(names.values())
    # 1語で登録されているキーワード（例: blokees）や、店舗カテゴリ名にあるブランド（例: 資生堂・ELIXIR）はブランド名として扱う
    brand_like = set()
    for kws in registered.values():
        brand_like |= {k for k in kws if " " not in k}
    aliases = script_aliases(names.values())
    item_cats = {}
    for iid, path in cat_map.items():
        cats, brands = category_terms(path)
        item_cats[iid] = cats
        brand_like |= brands

    candidates: Dict[str, Dict[str, dict]] = {}

    def offer(iid, kw, score, exp_vpc, reason, impact=""):
        key = normalize_keyword(kw)
        if not key or key in registered.get(iid, set()):
            return
        cur = candidates.setdefault(iid, {})
        if key not in cur or cur[key]["score"] < score:
            cur[key] = {"キーワード": kw, "score": score, "exp_vpc": exp_vpc, "根拠": reason, "影響": impact}

    # 1) 検索キーワードデータ（需要が実在する語）
    if search_kw is not None and not search_kw.empty and "キーワード" in search_kw:
        sk = search_kw.sort_values("アクセス数", ascending=False).head(500) if "アクセス数" in search_kw else search_kw
        for _, q in sk.iterrows():
            kw = str(q["キーワード"]).strip()
            if not kw or kw.lower() == "nan":
                continue
            acc = float(q.get("アクセス数") or 0) if pd.notna(q.get("アクセス数")) else 0.0
            sales = float(q.get("売上金額") or 0) if pd.notna(q.get("売上金額")) else 0.0
            kw_vpc = sales / acc if acc > 0 else np.nan
            targets = [str(q["商品管理番号"]).strip()] if "商品管理番号" in q and pd.notna(q.get("商品管理番号")) \
                and str(q.get("商品管理番号")).strip() else [i for i, n in names.items() if relevance(kw, n) >= 0.999]
            for iid in targets:
                if iid not in names:
                    continue
                item_vpc = vpc_map[iid]
                exp = item_vpc if math.isnan(kw_vpc) else 0.5 * item_vpc + 0.5 * kw_vpc
                score = 3.0 + math.log1p(acc) + (2.0 if sales > 0 else 0.0)
                offer(iid, kw, score, exp, f"検索キーワードデータで流入{acc:.0f}件・売上{_fmt(sales)}円の実績",
                      impact=f"店舗への流入{acc:.0f}件")

    # 2) 他の商品で売れているキーワードを横展開
    if kw_rep is not None and not kw_rep.empty:
        winners = kw_rep[kw_rep["売上件数"] > 0]
        for _, w in winners.iterrows():
            kw_vpc = w["売上金額"] / w["クリック数"] if w["クリック数"] > 0 else np.nan
            for iid, n in names.items():
                if iid == w["商品管理番号"] or relevance(w["キーワード"], n) < 0.999:
                    continue
                item_vpc = vpc_map[iid]
                exp = item_vpc if math.isnan(kw_vpc) else 0.6 * kw_vpc + 0.4 * item_vpc
                offer(iid, w["キーワード"], 2.5 + float(w["売上件数"]), exp,
                      f"別商品（{w['商品管理番号']}）で{w['売上件数']:.0f}件受注した語。商品名にも含まれるため横展開")

    # 3) 商品名から作る組み合わせ
    for iid, n in names.items():
        for kw, score, reason in title_candidates(n, doc_freq, len(names), extra_anchors=brand_like,
                                                  extra_categories=item_cats.get(iid, set()), aliases=aliases):
            offer(iid, kw, score, vpc_map[iid], reason)

    out: List[dict] = []
    log: List[dict] = []
    item_idx = items.set_index("商品管理番号")
    for iid, cands in candidates.items():
        it = item_idx.loc[iid]
        cls = it["分類"]
        target = float(it["目標ROAS(%)"])
        slots = max(s.max_kw_per_item - len(registered.get(iid, set())), 0)
        limit = min(slots, s.per_item_new)
        picked = 0
        for c in _diverse_order(list(cands.values())):
            cpc_raw = c["exp_vpc"] / (target / 100.0)
            status = ""
            if cls == CLS_STRUCT and not s.allow_unprofitable_new:
                status = "見送り（商品が構造的赤字。先にページ改善が必要）"
            elif math.isnan(target) or math.isnan(c["exp_vpc"]):
                status = "見送り（価格が分からず採算を計算できない）"
            elif picked >= limit:
                status = "見送り（今回の追加上限・登録枠）"
            elif cpc_raw < s.min_kw_cpc and not s.allow_unprofitable_new and cls == CLS_NEW:
                status = (f"待機（推定の採算CPC {cpc_raw:.0f}円が下限{s.min_kw_cpc}円未満。"
                          "商品CPCで実績を取り、CVRが確認できたら投入）")
            elif cpc_raw < s.min_kw_cpc and not s.allow_unprofitable_new:
                status = f"見送り（採算CPC {cpc_raw:.0f}円が下限{s.min_kw_cpc}円未満）"
            cpc = int(round(_clamp(max(cpc_raw, s.min_kw_cpc), s.min_kw_cpc, min(s.new_kw_max_cpc, s.max_cpc))))
            if not status:
                status = "採用"
                picked += 1
                out.append({
                    "商品管理番号": iid, "キーワード": c["キーワード"], "CPC": cpc,
                    "根拠": f"{c['根拠']}。想定1クリック売上{c['exp_vpc']:.0f}円・目標ROAS{target:.0f}%からテスト用CPCを算出"
                            "（次回の運用で成果を見て継続・調整・削除）",
                    "影響": c["影響"] or "テスト",
                })
            log.append({"商品管理番号": iid, "商品名": it["商品名"], "キーワード": c["キーワード"],
                        "スコア": round(c["score"], 2), "想定1クリック売上": round(c["exp_vpc"], 1),
                        "採算CPC": round(cpc_raw, 1), "テストCPC": cpc, "根拠": c["根拠"], "判定": status})
    return out, pd.DataFrame(log)


ITEM_COLUMNS = ["商品管理番号", "商品名", "クリック数", "実績額", "売上金額", "売上件数", "評価売上", "粗利率",
                "目標ROAS(%)", "客単価", "1クリック売上", "目標CPC", "分類", "CVR(%)", "ROAS(%)", "粗利-広告費"]


def _brand_terms(universe: pd.DataFrame, registered: Dict[str, set]) -> set:
    """ブランド・作品名らしい語: 店舗カテゴリのブランド名、1語で登録済みのキーワード、複数商品の商品名に共通する固有の語"""
    from collections import Counter

    from .keywords import ATTRIBUTE_WORDS, CATEGORY_WORDS, tokenize

    terms = set()
    for kws in registered.values():
        terms |= {k for k in kws if " " not in k}
    if "カテゴリ" in universe:
        for path in universe["カテゴリ"].astype(str):
            terms |= category_terms(path)[1]
    import re

    toks = [{t.lower() for t in tokenize(n)} for n in universe["商品名"].astype(str)]
    df: Counter = Counter()
    for ts in toks:
        df.update(ts)
    # 英字のブランド名（例: blokees, dhc, elixir）
    for t, c in df.items():
        if c >= 2 and re.fullmatch(r"[a-z][a-z0-9&.\-]{2,}", t) and t not in CATEGORY_WORDS:
            terms.add(t)
    # ブランドの商品にほぼ必ず一緒に出てくる語（例: Blokees と一緒に出る マーベル・スターウォーズ ＝ 作品名）
    anchors = set(terms)
    for t, c in df.items():
        if c < 3 or t in CATEGORY_WORDS or t in ATTRIBUTE_WORDS or t in anchors:
            continue
        with_brand = sum(1 for ts in toks if t in ts and ts & anchors)
        if with_brand >= 0.8 * c:
            terms.add(t)
    return terms


def _empty_items() -> pd.DataFrame:
    df = pd.DataFrame(columns=ITEM_COLUMNS + ["_source"])
    df.attrs["store_vpc"] = np.nan
    df.attrs["store_aov"] = np.nan
    return df


def build_universe(items: pd.DataFrame, catalog: Optional[pd.DataFrame], master: Optional[pd.DataFrame],
                   s: Strategy) -> pd.DataFrame:
    """RPPの実績がある商品 ＋ 商品一覧にしか無い商品 を1つの表にする（全商品をゼロベースで設計するため）。

    実績の無い商品の1クリック売上 ＝ 販売価格 × 想定CVR
      販売価格: 商品一覧の価格 → 店舗カテゴリの価格（500円均一など）→ 店舗の平均客単価 の順に使う
      想定CVR : 店舗のRPP実績のCVRを、設定の想定CVRに寄せて補正した値
    """
    u = items.copy()
    if "カテゴリ" not in u:
        u["カテゴリ"] = ""
    u["価格の出所"] = "RPP実績"
    clicks = pd.to_numeric(u["クリック数"], errors="coerce").sum() if not u.empty else 0
    orders = pd.to_numeric(u["売上件数"], errors="coerce").sum() if not u.empty else 0
    k = 300.0
    store_cvr = (orders + s.assumed_cvr / 100.0 * k) / (clicks + k)
    store_aov = items.attrs.get("store_aov", np.nan)

    if catalog is None or catalog.empty:
        u.attrs["store_cvr"] = store_cvr
        return u

    cat = catalog.copy()
    cat_by_id = cat.set_index("商品管理番号")
    # 実績がある商品にも店舗カテゴリを付ける
    u["カテゴリ"] = u["商品管理番号"].map(cat_by_id["カテゴリ"]).fillna("") if "カテゴリ" in cat_by_id else ""
    known_prices = cat["販売価格"].dropna()
    if (store_aov is None or (isinstance(store_aov, float) and math.isnan(store_aov))) and not known_prices.empty:
        store_aov = float(known_prices.median())

    m = None
    if master is not None and not master.empty:
        m = master.drop_duplicates("商品管理番号", keep="last").set_index("商品管理番号")

    rows = []
    for _, c in cat[~cat["商品管理番号"].isin(set(u["商品管理番号"]))].iterrows():
        iid = c["商品管理番号"]
        margin = c.get("粗利率")
        if m is not None and iid in m.index and "粗利率" in m and pd.notna(m.loc[iid, "粗利率"]):
            margin = m.loc[iid, "粗利率"]
        margin = float(margin) if pd.notna(margin) and margin > 0 else s.default_margin
        target = c.get("目標ROAS")
        if m is not None and iid in m.index and "目標ROAS" in m and pd.notna(m.loc[iid, "目標ROAS"]):
            target = m.loc[iid, "目標ROAS"]
        target = float(target) if pd.notna(target) and target > 0 else 100.0 / (margin / 100.0) * s.mult
        if pd.notna(c.get("販売価格")) and c["販売価格"] > 0:
            price, src = float(c["販売価格"]), "商品一覧の価格"
        elif pd.notna(c.get("価格の目安")) and c["価格の目安"] > 0:
            price, src = float(c["価格の目安"]), "店舗カテゴリの価格"
        else:
            price, src = store_aov, "店舗平均（推定）"
        vpc = price * store_cvr if price is not None and not math.isnan(price) else np.nan
        rows.append({
            "商品管理番号": iid, "商品名": c["商品名"], "カテゴリ": c.get("カテゴリ", ""), "クリック数": 0, "実績額": 0,
            "売上金額": 0, "売上件数": 0, "評価売上": 0, "粗利率": margin, "目標ROAS(%)": round(target, 0),
            "客単価": price, "1クリック売上": vpc,
            "目標CPC": round(vpc / (target / 100.0), 1) if not math.isnan(vpc) else np.nan,
            "分類": CLS_NEW, "価格の出所": src,
        })
    if rows:
        u = pd.concat([u, pd.DataFrame(rows)], ignore_index=True)
    u.attrs["store_cvr"] = store_cvr
    return u


def _portfolio(universe: pd.DataFrame, act: pd.DataFrame, registered: Dict[str, set], s: Strategy) -> pd.DataFrame:
    """全商品の投資区分（攻め／守り／停止候補）と、キーワードの登録状況"""
    if universe.empty:
        return pd.DataFrame()
    new_cnt = act[act["施策"] == KW_NEW].groupby("商品管理番号").size()
    del_cnt = act[(act["施策"] == KW_DELETE) & act["実行"]].groupby("商品管理番号").size()
    rows = []
    for _, u in universe.iterrows():
        iid = u["商品管理番号"]
        tcpc = u.get("目標CPC")
        cls = u.get("分類")
        if pd.isna(tcpc):
            inv, why = INV_DEFEND, "価格が分からないため商品CPCのみで様子見"
        elif cls == CLS_NEW and tcpc < s.min_kw_cpc:
            inv, why = INV_TEST, (f"RPPの実績なし。推定の採算CPC {tcpc:.0f}円（価格×想定CVR）ではキーワード下限に届かないため、"
                                  "まず商品CPCで少額配信し、実際のCVRを測る")
        elif cls == CLS_STRUCT:
            inv, why = INV_DEFEND, "構造的赤字。ページ改善でCVRが上がるまでキーワード投資はしない"
        elif tcpc >= s.min_kw_cpc:
            inv, why = INV_ATTACK, f"採算CPC {tcpc:.0f}円 ≥ キーワード下限{s.min_kw_cpc}円"
        elif tcpc >= s.min_item_cpc:
            inv, why = INV_DEFEND, f"採算CPC {tcpc:.0f}円。キーワード下限{s.min_kw_cpc}円では赤字になるため商品CPCで低く配信"
        else:
            inv, why = INV_STOP, f"採算CPC {tcpc:.0f}円が商品CPCの下限{s.min_item_cpc}円未満。除外を検討"
        reg = len(registered.get(iid, set()))
        rows.append({
            "投資区分": inv, "商品管理番号": iid, "商品名": u["商品名"], "分類": cls,
            "価格": u.get("客単価"), "価格の出所": u.get("価格の出所"), "粗利率": u.get("粗利率"),
            "目標ROAS(%)": u.get("目標ROAS(%)"), "1クリック売上": None if pd.isna(u.get("1クリック売上")) else round(float(u["1クリック売上"]), 1),
            "採算CPC": None if pd.isna(tcpc) else round(float(tcpc), 1),
            "登録済みKW": reg, "今回追加KW": int(new_cnt.get(iid, 0)), "今回削除KW": int(del_cnt.get(iid, 0)),
            "判断": why,
        })
    order = {INV_ATTACK: 0, INV_TEST: 1, INV_DEFEND: 2, INV_STOP: 3}
    return pd.DataFrame(rows).sort_values(["投資区分", "採算CPC"], key=lambda c: c.map(order) if c.name == "投資区分" else -c.fillna(-1)).reset_index(drop=True)


def _diverse_order(cands: List[dict]) -> List[dict]:
    """スコア順に並べつつ、同じ語ばかりの候補が続かないよう、既に選んだ語と重なる候補の順位を下げる"""
    from collections import Counter

    remaining = list(cands)
    used: Counter = Counter()
    ordered = []
    while remaining:
        def adj(c):
            toks = normalize_keyword(c["キーワード"]).split()
            return c["score"] - 0.6 * sum(used[t] for t in toks)
        best = max(remaining, key=adj)
        remaining.remove(best)
        ordered.append(best)
        used.update(normalize_keyword(best["キーワード"]).split())
    return ordered


def history_records(plan: Plan, period_days: Optional[int]) -> pd.DataFrame:
    """次回の運用で効果検証に使う記録（このCSVを次回読み込む）"""
    run_id = datetime.now().strftime("%Y%m%d%H%M")
    act = plan.actions[plan.actions["実行"]].copy()
    kw = plan.keywords
    stats = {}
    if not kw.empty:
        for _, r in kw.iterrows():
            stats[(r["商品管理番号"], normalize_keyword(r["キーワード"]))] = r
    recs = []
    for _, a in act.iterrows():
        st = stats.get((a["商品管理番号"], normalize_keyword(a["キーワード"] or "")))
        recs.append({
            "運用記録ID": run_id, "施策": a["施策"], "商品管理番号": a["商品管理番号"], "キーワード": a["キーワード"],
            "変更前CPC": a["変更前CPC"], "変更後CPC": a["変更後CPC"], "集計日数": period_days,
            "クリック数": st["クリック数"] if st is not None else None,
            "実績額": st["実績額"] if st is not None else None,
            "売上金額": st["売上金額"] if st is not None else None,
            "ROAS(%)": round(st["売上金額"] / st["実績額"] * 100, 1) if st is not None and st["実績額"] else None,
            "根拠": a["根拠"],
        })
    return pd.DataFrame(recs)
