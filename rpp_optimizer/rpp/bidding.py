"""CPC推奨値の計算ロジック

考え方
------
1. 1クリックあたりの売上（VPC: Value Per Click）＝ 売上金額 ÷ クリック数
2. 目標ROASを満たせる上限CPC ＝ VPC ÷ 目標ROAS
   例: VPC 300円・目標ROAS 400% → 300 ÷ 4.0 = 75円
3. クリック数が少ない商品・キーワードは数値がぶれやすいため、
   上位の平均値（キーワードなら商品全体、商品なら店舗全体）に寄せて補正する（ベイズ平滑化）。
4. 1回の調整幅は現在CPCの±一定割合に制限し、急激な変動を避ける。
5. 下限CPC・上限CPCの範囲に収め、1円単位に丸める。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from .io import to_number

# 判定区分
ACTION_UP = "引き上げ"
ACTION_DOWN = "引き下げ"
ACTION_KEEP = "据え置き"
ACTION_HOLD = "データ不足（据え置き）"
ACTION_REVIEW = "要確認（停止・除外を検討）"


@dataclass
class Settings:
    target_roas_pct: float = 400.0          # 目標ROAS(%)
    min_item_cpc: int = 20                  # 商品CPCの下限（円）※RMSの最新仕様を確認して設定
    min_keyword_cpc: int = 40               # キーワードCPCの下限（円）
    max_cpc: int = 300                      # 自社で許容する上限CPC（円）
    max_change_rate: float = 0.20           # 1回あたりの最大変更率（0.2 = ±20%）
    min_clicks: int = 30                    # 判定に必要な最低クリック数
    prior_clicks: float = 30.0              # 平滑化の強さ（仮想クリック数）
    no_sale_spend_limit: float = 3000.0     # 売上ゼロでこの広告費を超えたら引き下げ（円）
    keep_band: float = 0.05                 # 推奨値との差がこの割合以内なら据え置き
    margin_mode: bool = False               # True: 粗利率から損益分岐ROASを算出して目標に使う
    margin_safety: float = 1.2              # 損益分岐ROAS × この係数 を目標ROASとする
    explore_rate: float = 0.0               # データ不足時に試験的に上げる割合（0 = 上げない）


@dataclass
class ColumnMap:
    item_id: Optional[str] = None
    item_name: Optional[str] = None
    keyword: Optional[str] = None
    impressions: Optional[str] = None
    clicks: Optional[str] = None
    spend: Optional[str] = None
    actual_cpc: Optional[str] = None
    current_cpc: Optional[str] = None
    guide_cpc: Optional[str] = None
    sales: Optional[str] = None
    orders: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Optional[str]]) -> "ColumnMap":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


def _num(df: pd.DataFrame, col: Optional[str]) -> pd.Series:
    if col and col in df.columns:
        return to_number(df[col])
    return pd.Series([float("nan")] * len(df), index=df.index)


def normalize_report(df: pd.DataFrame, cmap: ColumnMap, level: str) -> pd.DataFrame:
    """レポートを共通の列名に揃え、同じ商品（・キーワード）の行を合算する。

    level: "item" または "keyword"
    """
    if not cmap.item_id or cmap.item_id not in df.columns:
        raise ValueError("商品管理番号の列が指定されていません")
    if level == "keyword" and (not cmap.keyword or cmap.keyword not in df.columns):
        raise ValueError("キーワードの列が指定されていません")
    if not cmap.clicks or not cmap.spend or not cmap.sales:
        raise ValueError("クリック数・実績額・売上金額の列は必須です")

    out = pd.DataFrame({"商品管理番号": df[cmap.item_id].astype(str).str.strip()})
    if level == "keyword":
        out["キーワード"] = df[cmap.keyword].astype(str).str.strip()
    out["商品名"] = df[cmap.item_name].astype(str) if cmap.item_name in df.columns else ""
    out["表示回数"] = _num(df, cmap.impressions)
    out["クリック数"] = _num(df, cmap.clicks).fillna(0)
    out["実績額"] = _num(df, cmap.spend).fillna(0)
    out["売上金額"] = _num(df, cmap.sales).fillna(0)
    out["売上件数"] = _num(df, cmap.orders).fillna(0)
    out["現在CPC"] = _num(df, cmap.current_cpc)
    out["CPC実績"] = _num(df, cmap.actual_cpc)
    out["目安CPC"] = _num(df, cmap.guide_cpc)

    # 合計行・空行を除外
    bad_ids = {"", "nan", "合計", "総計", "全体"}
    out = out[~out["商品管理番号"].isin(bad_ids)]
    if level == "keyword":
        out = out[~out["キーワード"].isin(bad_ids)]

    keys = ["商品管理番号"] + (["キーワード"] if level == "keyword" else [])
    agg = out.groupby(keys, as_index=False, sort=False).agg(
        商品名=("商品名", "first"),
        表示回数=("表示回数", lambda s: s.sum(min_count=1)),
        クリック数=("クリック数", "sum"),
        実績額=("実績額", "sum"),
        売上金額=("売上金額", "sum"),
        売上件数=("売上件数", "sum"),
        現在CPC=("現在CPC", "last"),
        目安CPC=("目安CPC", "last"),
    )
    # CPC実績は合算後に再計算（日別行の単純平均は不正確なため）
    agg["CPC実績"] = agg.apply(
        lambda r: r["実績額"] / r["クリック数"] if r["クリック数"] > 0 else float("nan"), axis=1
    )
    return agg


def attach_master(report: pd.DataFrame, master: Optional[pd.DataFrame]) -> pd.DataFrame:
    """商品マスタ（商品管理番号・粗利率・目標ROAS）を結合する"""
    report = report.copy()
    report["粗利率"] = float("nan")
    report["個別目標ROAS"] = float("nan")
    if master is None or master.empty:
        return report
    m = master.copy()
    m["商品管理番号"] = m["商品管理番号"].astype(str).str.strip()
    m = m.drop_duplicates("商品管理番号", keep="last").set_index("商品管理番号")
    if "粗利率" in m.columns:
        report["粗利率"] = report["商品管理番号"].map(m["粗利率"])
    if "目標ROAS" in m.columns:
        report["個別目標ROAS"] = report["商品管理番号"].map(m["目標ROAS"])
    return report


def _target_roas(row: pd.Series, s: Settings) -> float:
    if pd.notna(row.get("個別目標ROAS")) and row["個別目標ROAS"] > 0:
        return float(row["個別目標ROAS"])
    margin = row.get("粗利率")
    if s.margin_mode and pd.notna(margin) and margin > 0:
        # 粗利率30% → 損益分岐ROAS 333% → × 係数
        return 100.0 / (float(margin) / 100.0) * s.margin_safety
    return s.target_roas_pct


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def recommend(
    report: pd.DataFrame,
    s: Settings,
    level: str,
    prior_vpc: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """推奨CPCを計算する。

    report: normalize_report（+ attach_master）済みのデータ
    prior_vpc: 商品管理番号 → 1クリックあたり売上 の辞書（キーワード評価時の補正に使う）
    """
    df = report.copy()
    min_cpc = s.min_keyword_cpc if level == "keyword" else s.min_item_cpc
    max_cpc = max(s.max_cpc, min_cpc)

    total_clicks = df["クリック数"].sum()
    store_vpc = df["売上金額"].sum() / total_clicks if total_clicks > 0 else 0.0

    rows = []
    for _, r in df.iterrows():
        clicks = float(r["クリック数"])
        spend = float(r["実績額"])
        sales = float(r["売上金額"])
        target = _target_roas(r, s)
        target_ratio = target / 100.0

        prior = store_vpc
        if prior_vpc and r["商品管理番号"] in prior_vpc and not math.isnan(prior_vpc[r["商品管理番号"]]):
            prior = prior_vpc[r["商品管理番号"]]

        vpc_raw = sales / clicks if clicks > 0 else float("nan")
        vpc_smoothed = (sales + prior * s.prior_clicks) / (clicks + s.prior_clicks)
        ideal = vpc_smoothed / target_ratio if target_ratio > 0 else float("nan")

        # 基準となる現在CPC（設定値が無ければ実績CPC、それも無ければ理論値）
        base = r["現在CPC"]
        base_src = "設定CPC"
        if pd.isna(base) or base <= 0:
            base = r["CPC実績"]
            base_src = "CPC実績"
        if pd.isna(base) or base <= 0:
            base = _clamp(ideal if not math.isnan(ideal) else min_cpc, min_cpc, max_cpc)
            base_src = "理論値"
        base = float(base)

        lo = base * (1 - s.max_change_rate)
        hi = base * (1 + s.max_change_rate)
        roas = sales / spend * 100 if spend > 0 else float("nan")
        # 平滑化後の1クリック売上で見た見込みROAS（クリックが少ないほど平均値に寄る）
        avg_cpc = spend / clicks if clicks > 0 else float("nan")
        est_roas = vpc_smoothed / avg_cpc * 100 if avg_cpc and avg_cpc > 0 else float("nan")

        if sales <= 0 and spend >= s.no_sale_spend_limit:
            new = lo
            action = ACTION_DOWN
            reason = f"売上0円のまま広告費{spend:,.0f}円（基準{s.no_sale_spend_limit:,.0f}円以上）"
        elif clicks < s.min_clicks:
            if s.explore_rate > 0 and (math.isnan(roas) or roas >= target):
                new = base * (1 + s.explore_rate)
                action = ACTION_UP
                reason = f"クリック{clicks:.0f}回で判定前のため、露出確保に試験的に+{s.explore_rate:.0%}"
            else:
                new = base
                action = ACTION_HOLD
                reason = f"クリック{clicks:.0f}回（判定に必要な{s.min_clicks}回未満）"
        else:
            new = _clamp(ideal, lo, hi)
            diff = (ideal - base) / base if base > 0 else 0
            if abs(diff) <= s.keep_band:
                new = base
                action = ACTION_KEEP
                reason = f"見込みROAS {est_roas:.0f}%（実績{roas:.0f}%）が目標{target:.0f}%付近"
            elif diff > 0:
                action = ACTION_UP
                reason = f"見込みROAS {est_roas:.0f}%（実績{roas:.0f}%）が目標{target:.0f}%を上回るため、配信拡大の余地あり"
            else:
                action = ACTION_DOWN
                reason = f"見込みROAS {est_roas:.0f}%（実績{roas:.0f}%）が目標{target:.0f}%を下回る"

        new_rounded = int(round(_clamp(new, min_cpc, max_cpc)))
        if new_rounded == int(round(base)) and action in (ACTION_UP, ACTION_DOWN):
            action = ACTION_KEEP
            reason += "（上限/下限のため変更なし）"

        # 下限まで下げてもROASが目標の半分未満 → 停止・除外の検討対象
        if (
            new_rounded <= min_cpc
            and clicks >= s.min_clicks
            and (math.isnan(roas) or roas < target * 0.5)
        ):
            action = ACTION_REVIEW
            suffix = "キーワード削除" if level == "keyword" else "除外商品への登録"
            reason += f"／下限CPC({min_cpc}円)でも採算が合わない可能性。{suffix}を検討"

        rows.append(
            {
                "目標ROAS(%)": round(target, 1),
                "ROAS(%)": None if math.isnan(roas) else round(roas, 1),
                "見込みROAS(%)": None if math.isnan(est_roas) else round(est_roas, 1),
                "CVR(%)": round(r["売上件数"] / clicks * 100, 2) if clicks > 0 else None,
                "1クリック売上": None if math.isnan(vpc_raw) else round(vpc_raw, 1),
                "補正後1クリック売上": round(vpc_smoothed, 1),
                "理論上限CPC": None if math.isnan(ideal) else round(ideal, 1),
                "基準CPC": round(base, 1),
                "基準CPCの出所": base_src,
                "推奨CPC": new_rounded,
                "変更額": new_rounded - int(round(base)),
                "判定": action,
                "理由": reason,
            }
        )
    res = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    return res


def item_vpc_map(item_result: pd.DataFrame) -> Dict[str, float]:
    """商品別の（補正後）1クリック売上。キーワード評価の事前値として使う。"""
    return dict(zip(item_result["商品管理番号"], item_result["補正後1クリック売上"]))


def summarize(result: pd.DataFrame) -> Dict[str, float]:
    spend = result["実績額"].sum()
    sales = result["売上金額"].sum()
    clicks = result["クリック数"].sum()
    return {
        "件数": len(result),
        "広告費": spend,
        "売上": sales,
        "ROAS(%)": sales / spend * 100 if spend > 0 else float("nan"),
        "クリック数": clicks,
        "引き上げ": int((result["判定"] == ACTION_UP).sum()),
        "引き下げ": int((result["判定"] == ACTION_DOWN).sum()),
        "要確認": int((result["判定"] == ACTION_REVIEW).sum()),
    }
