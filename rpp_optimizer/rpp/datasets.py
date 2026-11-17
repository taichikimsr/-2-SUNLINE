"""読み込んだCSVを、種類ごとに運用エンジンで使う形に整える"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .bidding import ColumnMap, normalize_report
from .io import classify_dataset, detect_column, detect_columns, split_sales_columns, to_number

LABELS = {
    "item_report": "RPP 商品別レポート",
    "keyword_report": "RPP キーワード別レポート",
    "search_keywords": "検索キーワードデータ",
    "catalog": "商品一覧（全商品）",
    "master": "商品マスタ",
    "history": "前回の運用記録",
    "unknown": "判定できないファイル",
}


def prepare_report(df: pd.DataFrame, level: str, window: str = "720") -> pd.DataFrame:
    det = detect_columns(df.columns, prefer_window=window)
    det.update(split_sales_columns(df.columns, window))
    return normalize_report(df, ColumnMap.from_dict(det), level)


def prepare_master(df: pd.DataFrame) -> pd.DataFrame:
    det = detect_columns(df.columns)
    if not det["item_id"]:
        raise ValueError("商品マスタに「商品管理番号」列がありません")
    out = pd.DataFrame({"商品管理番号": df[det["item_id"]].astype(str).str.strip()})
    if det["margin_rate"]:
        out["粗利率"] = to_number(df[det["margin_rate"]])
    if det["target_roas"]:
        out["目標ROAS"] = to_number(df[det["target_roas"]])
    return out


def prepare_search_keywords(df: pd.DataFrame) -> pd.DataFrame:
    """検索キーワードデータ（列名は出力元によって異なるため別名で照合）"""
    kw_col = detect_column(df.columns, "keyword")
    if kw_col is None:
        kw_col = next((c for c in df.columns if "検索" in c and "ワード" in c), None)
    if kw_col is None:
        raise ValueError("検索キーワードデータに「キーワード」列がありません")
    out = pd.DataFrame({"キーワード": df[kw_col].astype(str).str.strip()})
    acc = detect_column(df.columns, "access")
    sales = detect_column(df.columns, "sales") or next((c for c in df.columns if "売上" in c), None)
    orders = detect_column(df.columns, "orders")
    item = detect_column(df.columns, "item_id")
    out["アクセス数"] = to_number(df[acc]) if acc else float("nan")
    out["売上金額"] = to_number(df[sales]) if sales else float("nan")
    out["売上件数"] = to_number(df[orders]) if orders else float("nan")
    if item:
        out["商品管理番号"] = df[item].astype(str).str.strip()
    out = out[(out["キーワード"] != "") & (out["キーワード"].str.lower() != "nan")]
    agg = {"アクセス数": "sum", "売上金額": "sum", "売上件数": "sum"}
    keys = ["キーワード"] + (["商品管理番号"] if item else [])
    return out.groupby(keys, as_index=False).agg(agg)


def detect_kind(df: pd.DataFrame) -> str:
    return classify_dataset(df)


def first_or_none(frames) -> Optional[pd.DataFrame]:
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def prepare_catalog(df: pd.DataFrame) -> pd.DataFrame:
    """商品一覧。販売価格が無い場合は 売上金額÷売上件数 を客単価として使う。粗利率・目標ROASの列があれば取り込む。"""
    det = detect_columns(df.columns)
    if not det["item_id"]:
        raise ValueError("商品一覧に「商品管理番号」列がありません")
    out = pd.DataFrame({"商品管理番号": df[det["item_id"]].astype(str).str.strip()})
    out["商品名"] = df[det["item_name"]].astype(str).str.strip() if det["item_name"] else ""
    price = None
    if det["price"]:
        price = to_number(df[det["price"]])
    elif det["sales"] and det["orders"]:
        price = to_number(df[det["sales"]]) / to_number(df[det["orders"]]).replace(0, float("nan"))
    out["販売価格"] = price if price is not None else float("nan")
    out["粗利率"] = to_number(df[det["margin_rate"]]) if det["margin_rate"] else float("nan")
    out["目標ROAS"] = to_number(df[det["target_roas"]]) if det["target_roas"] else float("nan")
    cat_col = next((c for c in df.columns if "カテゴリ" in c), None)
    out["カテゴリ"] = df[cat_col].astype(str).str.strip() if cat_col else ""
    out = out[(out["商品管理番号"] != "") & (out["商品管理番号"].str.lower() != "nan")]
    # SKU行などで同じ商品が複数行ある場合は、商品名が入っている行・最初の価格を使う
    out["商品名"] = out["商品名"].replace({"nan": ""})
    out = out.groupby("商品管理番号", as_index=False, sort=False).agg(
        商品名=("商品名", lambda x: next((v for v in x if v), "")),
        販売価格=("販売価格", "first"), 粗利率=("粗利率", "first"), 目標ROAS=("目標ROAS", "first"),
        カテゴリ=("カテゴリ", lambda x: " | ".join(dict.fromkeys(v for v in x if v and v != "nan"))),
    )
    # 店舗カテゴリ名に「500円均一」「1000円ポッキリ」などの価格があれば、販売価格が無い商品の目安にする
    hint = out["カテゴリ"].str.extract(r"(\d{3,6})\s*円")[0]
    out["価格の目安"] = pd.to_numeric(hint, errors="coerce")
    return out
