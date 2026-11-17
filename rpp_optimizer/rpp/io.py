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
    "guide_cpc": ["目安cpc"],
    "sales": ["売上金額"],
    "orders": ["売上件数"],
    "price": ["販売価格", "価格"],
    "margin_rate": ["粗利率", "利益率"],
    "target_roas": ["目標roas"],
    # RMS「検索キーワード」等の流入データ用
    "access": ["アクセス人数", "アクセス数", "訪問者数", "セッション数", "流入数", "ユニークユーザー数"],
    "conversion_rate": ["転換率", "cvr"],
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
    "guide_cpc": "目安CPC（楽天）",
    "sales": "売上金額",
    "orders": "売上件数",
    "price": "販売価格",
    "margin_rate": "粗利率(%)",
    "target_roas": "目標ROAS(%)",
    "access": "アクセス数",
    "conversion_rate": "転換率",
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


def _parse_rows(text: str) -> List[List[str]]:
    """カンマ区切り／タブ区切りを判定して全行を読む（行ごとに列数が違ってもエラーにしない）"""
    sample = "\n".join(text.splitlines()[:50])
    sep = "\t" if sample.count("\t") > sample.count(",") else ","
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=sep)]


def _find_header_row(rows: List[List[str]]) -> int:
    """ヘッダー行を探す。

    RMSのレポートは先頭に「期間」「検索条件」などの説明行が入ることがあるため、
    ・「商品管理番号」「キーワード」を含むセルがあり
    ・空でないセルが3つ以上ある
    最初の行をヘッダーとみなす。見つからなければ、列数が最も多い最初の行を使う。
    """
    for idx, row in enumerate(rows[:50]):
        cells = [normalize_header(c) for c in row if c.strip()]
        if len(cells) >= 3 and any(any(h in c for h in HEADER_HINTS) for c in cells):
            return idx
    widths = [len([c for c in r if c.strip()]) for r in rows[:50]]
    return widths.index(max(widths)) if widths else 0


def _unique(names: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out = []
    for i, n in enumerate(names):
        n = n or f"列{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        out.append(n)
    return out


def read_csv_bytes(data: bytes) -> pd.DataFrame:
    """RMS形式のCSVを読み込む（文字コード・ヘッダー行を自動判定）。値は文字列のまま返す。

    ヘッダーより前の説明行は読み飛ばし、ヘッダーより列数が多い／少ない行は切り詰め・空欄補完する。
    """
    text = decode_bytes(data)
    rows = _parse_rows(text)
    if not rows:
        raise ValueError("CSVが空です")
    h = _find_header_row(rows)
    header = _unique(rows[h])
    n = len(header)
    body = []
    for row in rows[h + 1:]:
        if not any(c.strip() for c in row):
            continue  # 空行
        body.append((row + [""] * n)[:n])
    df = pd.DataFrame(body, columns=header, dtype=str)
    # ヘッダーより前の説明行（「集計単位: キーワード別」「集計期間: …」など）を保持
    df.attrs["meta"] = [" ".join(c for c in r if c) for r in rows[:h] if any(c.strip() for c in r)]
    return df


_DATE_RANGE = re.compile(
    r"(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})日?\s*[-~～〜]\s*(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})"
)


def report_period_days(df: pd.DataFrame) -> Optional[int]:
    """説明行または「日付」列から集計期間の日数を求める（両端を含む）。分からなければ None。"""
    from datetime import date

    texts = list(df.attrs.get("meta", []))
    for c in df.columns:
        if "日付" in c or "期間" in c:
            texts += df[c].astype(str).head(3).tolist()
    for t in texts:
        m = _DATE_RANGE.search(unicodedata.normalize("NFKC", t))
        if m:
            y1, m1, d1, y2, m2, d2 = map(int, m.groups())
            try:
                return (date(y2, m2, d2) - date(y1, m1, d1)).days + 1
            except ValueError:
                continue
    return None


def split_sales_columns(columns: Iterable[str], window: str = "720") -> Dict[str, Optional[str]]:
    """新規・既存別の売上金額列を探す（例: 売上金額(新規720時間)）"""
    cols = list(columns)
    out: Dict[str, Optional[str]] = {"sales_new": None, "sales_existing": None}
    for key, word in (("sales_new", "新規"), ("sales_existing", "既存")):
        for c in cols:
            n = normalize_header(c)
            if "売上金額" in n and word in n and normalize_header(window) in n:
                out[key] = c
                break
    return out


def classify_dataset(df: pd.DataFrame) -> str:
    """読み込んだCSVの種類を判定する。

    keyword_report : RPPキーワード別レポート（商品管理番号＋キーワード＋クリック数）
    item_report    : RPP商品別レポート（商品管理番号＋クリック数、キーワード列なし）
    search_keywords: 検索キーワード等の流入データ（キーワード＋アクセス/売上、クリック数なし）
    catalog        : 商品一覧（商品管理番号＋商品名＋販売価格など。RMSの商品CSVやアクセス分析の商品別データ）
    master         : 商品マスタ（商品管理番号＋粗利率/目標ROAS）
    history        : 本ツールが出力した運用記録
    unknown        : 判定できない
    """
    cols = list(df.columns)
    norm = [normalize_header(c) for c in cols]
    has = lambda w: any(normalize_header(w) in n for n in norm)  # noqa: E731
    exact = lambda w: normalize_header(w) in norm  # noqa: E731
    if exact("運用記録id") or (exact("施策") and exact("変更後cpc")):
        return "history"
    if has("商品管理番号") and exact("キーワード") and has("クリック数"):
        return "keyword_report"
    if has("商品管理番号") and has("クリック数"):
        return "item_report"
    if (exact("キーワード") or exact("検索キーワード")) and (has("アクセス") or has("売上") or has("訪問")):
        return "search_keywords"
    if has("商品管理番号") and (has("価格") or (has("商品名") and has("売上"))):
        return "catalog"
    if has("商品管理番号") and (has("粗利率") or has("目標roas") or has("利益率")):
        return "master"
    if has("商品管理番号") and has("商品名"):
        return "catalog"
    return "unknown"


def detect_column(columns: Iterable[str], field: str, prefer: Optional[str] = None) -> Optional[str]:
    """field に該当する列名を推定する。prefer があればその文字列を含む列を優先（例: '720'）。"""
    cols = list(columns)
    norm = {c: normalize_header(c) for c in cols}
    for alias in COLUMN_ALIASES.get(field, []):
        a = normalize_header(alias)
        exact = [c for c in cols if norm[c] == a]
        if exact:
            return exact[0]
        hits = [c for c in cols if a in norm[c]]
        if field == "current_cpc":
            # 「CPC実績」「平均CPC」「目安CPC」は設定CPCではないので除外
            hits = [c for c in hits if not any(x in norm[c] for x in ("実績", "平均", "目安"))]
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


def to_csv_bytes(df: pd.DataFrame, encoding: str = "cp932", lineterminator: str = "\r\n") -> bytes:
    """CSVをバイト列で出力。cp932で表せない文字があればエラー内容を分かりやすく返す。

    RMSのRPP一括アップロード用サンプルCSVは Shift_JIS・改行LF・引用符なしのため、
    アップロード用は lineterminator="\n" で出力する。
    """
    text = df.to_csv(index=False, lineterminator=lineterminator)
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end]
        raise ValueError(
            f"文字コード {encoding} で表せない文字「{bad}」が含まれています。"
            "UTF-8で出力するか、該当の文字を修正してください。"
        ) from e
