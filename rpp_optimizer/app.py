"""楽天RPP広告 CPC最適化ツール（Streamlit）

使い方:
    streamlit run rpp_optimizer/app.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rpp.bidding import (  # noqa: E402
    ColumnMap,
    Settings,
    attach_master,
    item_vpc_map,
    normalize_report,
    recommend,
    summarize,
)
from rpp.export import build_upload  # noqa: E402
from rpp.io import FIELD_LABELS, detect_columns, read_csv_bytes, to_csv_bytes, to_number  # noqa: E402

st.set_page_config(page_title="RPP CPC最適化ツール", page_icon="📈", layout="wide")
st.title("📈 楽天RPP広告 CPC最適化ツール")
st.caption("RMSからダウンロードしたRPPレポートをもとに、商品ごと・キーワードごとの推奨CPCを算出します。")

with st.expander("📖 使い方", expanded=False):
    st.markdown(
        """
1. RMS「広告・アフィリエイト・楽天大学」→「広告（プロモーションメニュー）」→ RPP のパフォーマンスレポートから、
   **商品別レポート** と（必要に応じて）**キーワード別レポート** をCSVでダウンロードします。
   - 集計期間は直近14〜30日程度を推奨します（短すぎると判定できる商品が少なくなります）。
2. 左のサイドバーで目標ROASや下限CPCなどを設定します。
3. CSVをアップロードし、列の対応が正しいか確認します。
4. 推奨CPCを確認・必要に応じて手修正し、一括アップロード用CSVをダウンロードします。
5. RMSのRPP「商品・キーワード設定」から一括アップロードで反映します。

**注意**
- 楽天はRPPの入札を変更する公開APIを提供していないため（2026年11月時点で確認できた範囲）、本ツールは自動反映を行いません。
- 下限CPC・CSVの列構成は楽天の仕様変更で変わることがあります。必ずRMS画面の最新の表示・サンプルフォーマットと照合してください。
- 売上金額は12時間・720時間など計測期間の違う列があります。どちらを使うかサイドバーで選んでください。
"""
    )

# ---------------- サイドバー: 設定 ----------------
sb = st.sidebar
sb.header("⚙️ 運用方針")
target_roas = sb.number_input("目標ROAS（%）", min_value=50.0, max_value=5000.0, value=400.0, step=50.0,
                              help="売上金額 ÷ 広告費 × 100。この値を満たせる範囲で最大のCPCを推奨します。")
margin_mode = sb.checkbox("商品マスタの粗利率から目標ROASを自動算出", value=False,
                          help="損益分岐ROAS ＝ 100 ÷ 粗利率。これに下の係数を掛けた値を目標にします。")
margin_safety = sb.number_input("損益分岐ROASに掛ける係数", min_value=1.0, max_value=5.0, value=1.2, step=0.1,
                                disabled=not margin_mode)

sb.header("💴 CPCの範囲")
min_item_cpc = sb.number_input("商品CPCの下限（円）", min_value=1, value=20, step=1,
                               help="2025年の仕様変更で10円→20円になったとの情報があります。RMSで最新の値を確認してください。")
min_kw_cpc = sb.number_input("キーワードCPCの下限（円）", min_value=1, value=40, step=1)
max_cpc = sb.number_input("自社の上限CPC（円）", min_value=1, value=300, step=10)
max_change = sb.slider("1回あたりの最大変更幅（±%）", 5, 100, 20, 5) / 100

sb.header("📊 判定基準")
window = sb.radio("売上の計測期間", ["720時間", "12時間"], index=0, horizontal=True,
                  help="レポートの「売上金額(合計720時間)」などの列を自動で選びます。")
min_clicks = sb.number_input("判定に必要な最低クリック数", min_value=1, value=30, step=5)
prior_clicks = sb.number_input("平滑化の強さ（仮想クリック数）", min_value=0.0, value=30.0, step=5.0,
                               help="クリックが少ない商品の実績を平均値に寄せる度合い。大きいほど慎重になります。")
no_sale_limit = sb.number_input("売上0円で引き下げる広告費の基準（円）", min_value=0, value=3000, step=500)
keep_band = sb.slider("据え置きとする差の幅（±%）", 0, 30, 5) / 100
explore = sb.slider("データ不足時に試験的に上げる幅（%）", 0, 30, 0) / 100

settings = Settings(
    target_roas_pct=target_roas,
    min_item_cpc=int(min_item_cpc),
    min_keyword_cpc=int(min_kw_cpc),
    max_cpc=int(max_cpc),
    max_change_rate=max_change,
    min_clicks=int(min_clicks),
    prior_clicks=float(prior_clicks),
    no_sale_spend_limit=float(no_sale_limit),
    keep_band=keep_band,
    margin_mode=margin_mode,
    margin_safety=margin_safety,
    explore_rate=explore,
)

sb.header("📤 出力")
out_encoding = sb.radio("出力文字コード", ["cp932", "utf-8-sig"], index=0, horizontal=True,
                        format_func=lambda x: "Shift_JIS" if x == "cp932" else "UTF-8(BOM付)")
only_changed = sb.checkbox("変更がある行だけ出力", value=True)
include_review = sb.checkbox("「要確認」の行も下限CPCで出力", value=False)


# ---------------- 共通関数 ----------------
def load(uploaded):
    if uploaded is None:
        return None
    try:
        return read_csv_bytes(uploaded.getvalue())
    except Exception as e:  # noqa: BLE001
        st.error(f"{uploaded.name} を読み込めませんでした: {e}")
        return None


def column_selector(df: pd.DataFrame, fields, key: str) -> ColumnMap:
    detected = detect_columns(df.columns, prefer_window=window.replace("時間", ""))
    options = ["（なし）"] + list(df.columns)
    cols = st.columns(4)
    chosen = {}
    for i, f in enumerate(fields):
        default = detected.get(f)
        idx = options.index(default) if default in options else 0
        sel = cols[i % 4].selectbox(FIELD_LABELS[f], options, index=idx, key=f"{key}_{f}")
        chosen[f] = None if sel == "（なし）" else sel
    return ColumnMap.from_dict(chosen)


def show_summary(result: pd.DataFrame):
    s = summarize(result)
    c = st.columns(6)
    c[0].metric("対象数", f"{s['件数']:,}")
    c[1].metric("広告費", f"{s['広告費']:,.0f}円")
    c[2].metric("売上", f"{s['売上']:,.0f}円")
    c[3].metric("ROAS", "-" if pd.isna(s["ROAS(%)"]) else f"{s['ROAS(%)']:,.0f}%")
    c[4].metric("引き上げ / 引き下げ", f"{s['引き上げ']} / {s['引き下げ']}")
    c[5].metric("要確認", f"{s['要確認']}")


DISPLAY_COLS_ITEM = ["商品管理番号", "商品名", "クリック数", "実績額", "売上金額", "売上件数", "ROAS(%)", "見込みROAS(%)",
                     "CVR(%)", "目標ROAS(%)", "目安CPC", "基準CPC", "基準CPCの出所", "理論上限CPC", "推奨CPC", "変更額",
                     "判定", "理由"]
DISPLAY_COLS_KW = DISPLAY_COLS_ITEM[:1] + ["キーワード"] + DISPLAY_COLS_ITEM[1:]


def result_editor(result: pd.DataFrame, level: str, key: str) -> pd.DataFrame:
    cols = DISPLAY_COLS_KW if level == "keyword" else DISPLAY_COLS_ITEM
    cols = [c for c in cols if c in result.columns]
    judge_filter = st.multiselect("判定で絞り込み", sorted(result["判定"].unique()), key=f"{key}_filter")
    view = result if not judge_filter else result[result["判定"].isin(judge_filter)]
    edited = st.data_editor(
        view[cols],
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in cols if c != "推奨CPC"],
        column_config={
            "推奨CPC": st.column_config.NumberColumn("推奨CPC（編集可）", min_value=1, step=1, format="%d"),
            "実績額": st.column_config.NumberColumn(format="%d"),
            "売上金額": st.column_config.NumberColumn(format="%d"),
        },
        key=f"{key}_editor",
    )
    # 手修正を反映
    merged = result.copy()
    merged.loc[edited.index, "推奨CPC"] = edited["推奨CPC"].astype(int)
    merged["変更額"] = merged["推奨CPC"] - merged["基準CPC"].round().astype(int)
    return merged


def download_buttons(result: pd.DataFrame, level: str, template_file, key: str, report_df=None):
    label = "キーワード" if level == "keyword" else "商品"
    template = load(template_file) if template_file is not None else None
    if template is None and report_df is not None:
        # RMSのRPPレポートは説明行を消せばそのまま一括設定に使える形式のため、レポート自体をテンプレートにする
        template = report_df
        st.caption("テンプレート未指定のため、読み込んだレポートの列名で出力します。")
    try:
        upload = build_upload(result, level, template, only_changed, include_review)
    except ValueError as e:
        st.error(str(e))
        return
    min_cpc = settings.min_keyword_cpc if level == "keyword" else settings.min_item_cpc
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**一括アップロード用CSV（{len(upload)}行）**")
        cpc_col = [c for c in upload.columns if "cpc" in c.lower()]
        if cpc_col and len(upload) and (to_number(upload[cpc_col[0]]) < min_cpc).any():
            st.warning(f"下限CPC（{min_cpc}円）未満の値があります。")
        try:
            data = to_csv_bytes(upload, out_encoding, lineterminator="\n")
            st.download_button(f"⬇️ {label}CPC 一括アップロード用CSV", data,
                               file_name=f"rpp_{level}_cpc_upload_{stamp}.csv", mime="text/csv",
                               key=f"{key}_dl_up", use_container_width=True)
        except ValueError as e:
            st.error(str(e))
        if "コントロールカラム" in upload.columns:
            st.caption("コントロールカラム: レポートにある（登録済みの）行は u（CPC変更）、無い行は n（新規登録）。"
                       "d（削除）は出力しません。")
        st.caption("⚠️ このCSVをExcelで開いて上書き保存しないでください。商品管理番号（13桁の数字など）が"
                   "「4.57E+12」のような形に変わり、正しく登録できなくなります。")
        st.dataframe(upload.head(50), use_container_width=True, hide_index=True)
    with c2:
        st.markdown("**分析結果（記録用・Excelで開けるUTF-8）**")
        st.download_button(f"⬇️ {label}別 分析結果CSV", to_csv_bytes(result, "utf-8-sig"),
                           file_name=f"rpp_{level}_analysis_{stamp}.csv", mime="text/csv",
                           key=f"{key}_dl_an", use_container_width=True)
        st.caption("変更の履歴として保存しておくと、次回の効果検証に使えます。")


# ---------------- 商品マスタ ----------------
st.subheader("① 商品マスタ（任意）")
st.caption("列: 商品管理番号, 粗利率（%）, 目標ROAS（%）。粗利率または目標ROASを商品ごとに変えたい場合のみ使います。")
master_file = st.file_uploader("商品マスタCSV", type=["csv"], key="master")
master = None
mdf = load(master_file)
if mdf is not None:
    det = detect_columns(mdf.columns)
    if not det["item_id"]:
        st.error("商品マスタに「商品管理番号」列が見つかりません。")
    else:
        master = pd.DataFrame({"商品管理番号": mdf[det["item_id"]]})
        if det["margin_rate"]:
            master["粗利率"] = to_number(mdf[det["margin_rate"]])
        if det["target_roas"]:
            master["目標ROAS"] = to_number(mdf[det["target_roas"]])
        st.success(f"{len(master)}件の商品マスタを読み込みました。")

# ---------------- 商品別 ----------------
st.subheader("② 商品CPC")
item_file = st.file_uploader("RPP 商品別レポートCSV", type=["csv"], key="item")
item_template = st.file_uploader("（任意）RMSの商品CPC一括アップロード用サンプル／現在設定CSV", type=["csv"],
                                 key="item_tmpl",
                                 help="読み込むと、その列構成のままCPC列だけを書き換えて出力します。")
item_result = None
idf = load(item_file)
if idf is not None:
    with st.expander("列の対応（自動判定・変更可）", expanded=False):
        cmap = column_selector(idf, ["item_id", "item_name", "clicks", "spend", "sales", "orders",
                                     "current_cpc", "guide_cpc", "actual_cpc"], "item")
    if detect_columns(idf.columns).get("keyword") == "キーワード":
        st.error("このファイルは「キーワード別」レポートのため、②では計算しません。"
                 "下の③に読み込んでください。②には集計単位「商品別」のレポートを読み込みます。")
    else:
        try:
            rep = attach_master(normalize_report(idf, cmap, "item"), master)
            item_result = recommend(rep, settings, "item")
            show_summary(item_result)
            item_result = result_editor(item_result, "item", "item")
            download_buttons(item_result, "item", item_template, "item", idf)
        except ValueError as e:
            st.error(str(e))

# ---------------- キーワード別 ----------------
st.subheader("③ キーワードCPC")
st.caption("商品別レポートも読み込んでいる場合、クリックの少ないキーワードは商品全体の実績を基準に補正します。")
kw_file = st.file_uploader("RPP キーワード別レポートCSV", type=["csv"], key="kw")
kw_template = st.file_uploader("（任意）RMSのキーワード一括アップロード用サンプル／現在設定CSV", type=["csv"],
                               key="kw_tmpl")
kdf = load(kw_file)
if kdf is not None:
    with st.expander("列の対応（自動判定・変更可）", expanded=False):
        kmap = column_selector(kdf, ["item_id", "keyword", "item_name", "clicks", "spend", "sales", "orders",
                                     "current_cpc", "guide_cpc", "actual_cpc"], "kw")
    try:
        rep = attach_master(normalize_report(kdf, kmap, "keyword"), master)
        if item_result is not None:
            names = dict(zip(item_result["商品管理番号"], item_result["商品名"]))
            blank = rep["商品名"].fillna("").astype(str).str.strip().isin(["", "nan"])
            rep.loc[blank, "商品名"] = rep.loc[blank, "商品管理番号"].map(names).fillna("")
        prior = item_vpc_map(item_result) if item_result is not None else None
        kw_result = recommend(rep, settings, "keyword", prior_vpc=prior)
        show_summary(kw_result)
        kw_result = result_editor(kw_result, "keyword", "kw")
        download_buttons(kw_result, "keyword", kw_template, "kw", kdf)
    except ValueError as e:
        st.error(str(e))

st.markdown("---")
st.caption("RPP CPC最適化ツール v0.1 | SUNLINE合同会社")
