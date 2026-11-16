"""CSV読み込み・列名の自動判定

RMSからダウンロードするCSVは Shift_JIS(CP932) が多く、レポートによっては
ヘッダー行の前に期間などの説明行が入る場合があるため、
エンコーディングとヘッダー行の位置を自動で判定する。
列名はレポートの種類や仕様変更で変わる可能性があるため、別名(エイリアス)で照合し、
画面上で手動で選び直せるようにしている。
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Dict, Iterable, List, Optional

import pandas as pd

ENCODINGS = ("utf-8-sig", "cp932", "utf-8")

# 正規化後の列名で部分一致させる候補（先頭ほど優先）
COLUMN_ALIASES: Dict[str, List[str]] = {
    "item_id": ["商品管理番号"],
    "item_name": ["商品名"],
    "keyword": ["キーワード"],
    "impressions": ["表示回数", "インプレッション", "imp"],
    "clicks": ["クリック数"],
    "spend": ["実績額", "広告費", "消化金額", "利用金額"],
    "actual_cpc": ["cpc実績", "平均cpc", "実績cpc"],
    "current_cpc": ["設定cpc", "入札cpc", "入札単価", "キーワードcpc", "商品cpc", "cpc"],
    "sales": ["売上金額"],
    "orders": ["売上件数"],
    "price": ["販売価格", "価格"],
    "margin_rate": ["粗利率", "利益率"],
    "target_roas": ["目標roas"],
}

FIELD_LABELS: Dict[str, str] = {
    "item_id": "商品管理番号",
    "item_name": "商品名",
    "keyword": "キーワード",
    "impressions": "表示回数",
    "clicks": "クリック数",
    "spend": "実績額（広告費）",
    "actual_cpc": "CPC実績",
    "current_cpc": "現在の設定CPC",
    "sales": "売上金額",
    "orders": "売上件数",
    "price": "販売価格",
    "margin_rate": "粗利率(%)",
    "target_roas": "目標ROAS(%)",
}

HEADER_HINTS = ("商品管理番号", "キーワード")


def normalize_header(name: str) -> str:
    """全角/半角・空白・大文字小文字の違いを吸収した比較用の文字列を返す"""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    return re.sub(r"\s+", "", s)


def decode_bytes(data: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判定できませんでした（UTF-8 / Shift_JIS 以外の可能性があります）")


def _find_header_row(lines: List[str]) -> int:
    for idx, line in enumerate(lines[:30]):
        norm = normalize_header(line)
        if any(h in norm for h in HEADER_HINTS):
            return idx
    return 0


def read_csv_bytes(data: bytes) -> pd.DataFrame:
    """RMS形式のCSVを読み込む（文字コード・ヘッダー行を自動判定）。値は文字列のまま返す。"""
    text = decode_bytes(data)
    lines = text.splitlines()
    header_idx = _find_header_row(lines)
    body = "\n".join(lines[header_idx:])
    try:
        dialect = csv.Sniffer().sniff(lines[header_idx] if lines else ",", delimiters=",\t")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    df = pd.read_csv(io.StringIO(body), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    # 全列が空の行（合計行の後の空行など）を除外
    df = df[~(df.apply(lambda r: all(str(v).strip() == "" for v in r), axis=1))]
    return df.reset_index(drop=True)


def detect_column(columns: Iterable[str], field: str, prefer: Optional[str] = None) -> Optional[str]:
    """field に該当する列名を推定する。prefer があればその文字列を含む列を優先（例: '720'）。"""
    cols = list(columns)
    norm = {c: normalize_header(c) for c in cols}
    for alias in COLUMN_ALIASES.get(field, []):
        a = normalize_header(alias)
        hits = [c for c in cols if a in norm[c]]
        if field == "current_cpc":
            # 「CPC実績」「平均CPC」は設定CPCではないので除外
            hits = [c for c in hits if not any(x in norm[c] for x in ("実績", "平均"))]
        if field == "clicks":
            hits = [c for c in hits if "率" not in norm[c]]
        if not hits:
            continue
        if prefer:
            preferred = [c for c in hits if normalize_header(prefer) in norm[c]]
            if preferred:
                hits = preferred
        # 「合計」を含む列があれば優先（新規/既存別の内訳列より合計を使う）
        totals = [c for c in hits if "合計" in norm[c]]
        return (totals or hits)[0]
    return None


def detect_columns(columns: Iterable[str], prefer_window: Optional[str] = None) -> Dict[str, Optional[str]]:
    cols = list(columns)
    result = {}
    for field in COLUMN_ALIASES:
        prefer = prefer_window if field in ("sales", "orders") else None
        result[field] = detect_column(cols, field, prefer)
    return result


def to_number(series: pd.Series) -> pd.Series:
    """'1,234円' '12.3%' '-' などを数値に変換。変換できない値は NaN。"""
    s = series.astype(str).map(lambda v: unicodedata.normalize("NFKC", v))
    s = s.str.replace(r"[,円%￥¥\s]", "", regex=True)
    s = s.replace({"": None, "-": None, "--": None, "nan": None})
    return pd.to_numeric(s, errors="coerce")


def to_csv_bytes(df: pd.DataFrame, encoding: str = "cp932") -> bytes:
    """CSVをバイト列で出力。cp932で表せない文字があればエラー内容を分かりやすく返す。"""
    text = df.to_csv(index=False, lineterminator="\r\n")
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end]
        raise ValueError(
            f"文字コード {encoding} で表せない文字「{bad}」が含まれています。"
            "UTF-8で出力するか、該当の文字を修正してください。"
        ) from e
