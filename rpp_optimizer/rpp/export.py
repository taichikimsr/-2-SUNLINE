"""RMS一括アップロード用CSVの作成

RMSの一括アップロードCSVの正確な列構成は、RMS画面からダウンロードできる
サンプルフォーマット（または現在の設定のダウンロードCSV）で確認する必要がある。
そのため、テンプレートCSVを読み込んだ場合は「その列構成のまま」CPC列だけを書き換えて出力する。
テンプレートが無い場合は簡易形式（商品管理番号・(キーワード)・CPC）で出力する。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .io import normalize_header


def _find(cols, keyword: str, exclude: tuple = ()) -> Optional[str]:
    for c in cols:
        n = normalize_header(c)
        if keyword in n and not any(x in n for x in exclude):
            return c
    return None


def build_upload(
    result: pd.DataFrame,
    level: str,
    template: Optional[pd.DataFrame] = None,
    only_changed: bool = True,
    include_review: bool = False,
) -> pd.DataFrame:
    """推奨CPCからアップロード用データを作る。

    include_review: 「要確認」の行も推奨CPC（下限値）で出力するか
    """
    df = result.copy()
    if not include_review:
        df = df[df["判定"] != "要確認（停止・除外を検討）"]
    if only_changed:
        df = df[df["変更額"] != 0]

    keys = ["商品管理番号"] + (["キーワード"] if level == "keyword" else [])

    if template is None or len(template.columns) == 0:
        cols = keys + ["CPC"]
        out = df[keys].copy()
        out["CPC"] = df["推奨CPC"].astype(int).values
        return out[cols].reset_index(drop=True)

    tcols = list(template.columns)
    id_col = _find(tcols, "商品管理番号")
    kw_col = None
    if level == "keyword":
        kw_col = next((c for c in tcols if normalize_header(c) == "キーワード"), None) or _find(
            tcols, "キーワード", exclude=("cpc", "一括")
        )
    # 書き込む列は「キーワードCPC」「商品CPC」を優先。「目安CPC」「CPC実績」には書き込まない
    preferred = "キーワードcpc" if level == "keyword" else "商品cpc"
    cpc_col = next((c for c in tcols if normalize_header(c) == preferred), None) or _find(
        tcols, "cpc", exclude=("実績", "平均", "目安")
    )
    missing = [n for n, c in (("商品管理番号", id_col), ("CPC", cpc_col)) if c is None]
    if level == "keyword" and kw_col is None:
        missing.append("キーワード")
    if missing:
        raise ValueError(f"テンプレートCSVに次の列が見つかりません: {', '.join(missing)}")

    tmpl = template.copy()
    tmpl["_key"] = tmpl[id_col].astype(str).str.strip()
    if kw_col:
        tmpl["_key"] += "\t" + tmpl[kw_col].astype(str).str.strip()
    existing = tmpl.drop_duplicates("_key", keep="last").set_index("_key")

    rows = []
    for _, r in df.iterrows():
        key = str(r["商品管理番号"])
        if kw_col:
            key += "\t" + str(r["キーワード"])
        if key in existing.index:
            row = existing.loc[key, tcols].to_dict()
        else:
            row = {c: "" for c in tcols}
            row[id_col] = r["商品管理番号"]
            if kw_col:
                row[kw_col] = r["キーワード"]
        row[cpc_col] = int(r["推奨CPC"])
        rows.append(row)
    return pd.DataFrame(rows, columns=tcols)
