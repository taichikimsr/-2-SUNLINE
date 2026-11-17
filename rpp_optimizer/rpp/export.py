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

    ctrl_col = _find(tcols, "コントロールカラム")

    rows = []
    for _, r in df.iterrows():
        key = str(r["商品管理番号"])
        if kw_col:
            key += "\t" + str(r["キーワード"])
        registered = key in existing.index
        if registered:
            row = existing.loc[key, tcols].to_dict()
        else:
            row = {c: "" for c in tcols}
            row[id_col] = r["商品管理番号"]
            if kw_col:
                row[kw_col] = r["キーワード"]
        if ctrl_col:
            # レポート（テンプレート）にある＝登録済み → u（CPC変更）。無いもの → n（新規登録）。d（削除）は出力しない
            # ※レポート上で「キーワードCPC」が空欄でも登録済みの場合がある（RMSで n を送ると「既に登録済」エラー）
            row[ctrl_col] = "u" if registered else "n"
        row[cpc_col] = int(r["推奨CPC"])
        rows.append(row)
    out = pd.DataFrame(rows, columns=tcols)
    if ctrl_col:
        # RMSのエラー結果CSVと同じ列（コントロールカラム・商品管理番号・(キーワード)・CPC）に絞る
        out = out[[c for c in (ctrl_col, id_col, kw_col, cpc_col) if c]]
    return out.reset_index(drop=True)


# ---------------------------------------------------------------- 自動運用（アクションプラン）からの出力
KW_UPLOAD_COLUMNS = ["コントロールカラム", "商品管理番号", "キーワード", "キーワードCPC"]


def keyword_upload_from_actions(actions: pd.DataFrame) -> pd.DataFrame:
    """キーワードCPCの一括アップロード用データ（RMSのサンプルと同じ4列）。

    n: 新規登録 / u: CPC変更 / d: 削除（削除行のCPCは変更前の値を入れる。RMSのサンプルでも d 行にCPCがある）
    """
    a = actions[(actions["実行"]) & (actions["施策"].isin(["キーワード追加", "キーワードCPC変更", "キーワード削除"]))]
    rows = []
    for _, r in a.iterrows():
        cpc = r["変更前CPC"] if r["施策"] == "キーワード削除" else r["変更後CPC"]
        rows.append([r["コントロール"], r["商品管理番号"], r["キーワード"],
                     "" if pd.isna(cpc) else int(cpc)])
    return pd.DataFrame(rows, columns=KW_UPLOAD_COLUMNS)


def item_upload_from_actions(actions: pd.DataFrame, report_columns=None) -> pd.DataFrame:
    """商品CPCの一括アップロード用データ。

    商品別レポートの列名（コントロールカラム・商品管理番号・商品CPC）に合わせる。
    ※商品CPCの一括アップロード形式は、RMSのサンプルCSVで未確認。
    """
    cols = list(report_columns) if report_columns is not None else []
    ctrl = next((c for c in cols if normalize_header(c) == "コントロールカラム"), "コントロールカラム")
    id_col = next((c for c in cols if normalize_header(c) == "商品管理番号"), "商品管理番号")
    cpc = next((c for c in cols if normalize_header(c) == "商品cpc"), None) or _find(
        cols, "cpc", exclude=("実績", "平均", "目安", "キーワード")) or "商品CPC"
    a = actions[(actions["実行"]) & (actions["施策"] == "商品CPC変更")]
    return pd.DataFrame(
        [[r["コントロール"], r["商品管理番号"], int(r["変更後CPC"])] for _, r in a.iterrows()],
        columns=[ctrl, id_col, cpc],
    )


def exclusion_upload_from_actions(actions: pd.DataFrame) -> pd.DataFrame:
    """RPP除外商品の一括登録用データ。※RMSのサンプルCSVで列構成を未確認（解説記事では n:登録 / d:削除）。"""
    a = actions[(actions["実行"]) & (actions["施策"] == "除外候補")]
    return pd.DataFrame([["n", r["商品管理番号"]] for _, r in a.iterrows()], columns=["コントロールカラム", "商品管理番号"])
