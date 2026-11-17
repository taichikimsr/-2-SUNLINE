"""楽天RPP広告 自動運用ツール（Streamlit）

起動:
    py -m streamlit run app.py        （Windows）
    streamlit run rpp_optimizer/app.py
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rpp.datasets import (  # noqa: E402
    LABELS, detect_kind, first_or_none, prepare_catalog, prepare_master, prepare_report, prepare_search_keywords,
)
from rpp.export import item_upload_from_actions, keyword_upload_from_actions  # noqa: E402
from rpp.io import read_csv_bytes, report_period_days, to_csv_bytes  # noqa: E402
from rpp.strategy import (  # noqa: E402
    CLS_DATA, CLS_GROW, CLS_KEEP, CLS_NEW, CLS_STRUCT, CLS_TUNE, INV_ATTACK, INV_DEFEND, INV_STOP, INV_TEST,
    ITEM_CPC, ITEM_EXCLUDE, KW_DELETE, KW_NEW, KW_UPDATE, MODES, PAGE_FIX, Strategy, build_plan, history_records,
)

st.set_page_config(page_title="RPP自動運用", page_icon="📈", layout="wide")

st.markdown(
    """
<style>
.block-container {padding-top: 1.5rem;}
.kpi {border:1px solid rgba(128,128,128,.25); border-radius:10px; padding:12px 14px;}
.kpi .l {font-size:.8rem; opacity:.7;}
.kpi .v {font-size:1.5rem; font-weight:700;}
.insight {border-left:4px solid #bf0000; padding:8px 12px; margin:6px 0; background:rgba(191,0,0,.05);}
</style>
""",
    unsafe_allow_html=True,
)

st.title("📈 楽天RPP 自動運用")
st.caption("RMSのレポートを入れるだけで、診断 → 入札・キーワードの追加/削除 → RMS一括アップロード用CSV までを自動で作成します。")

# ================================================================ サイドバー：運用方針
sb = st.sidebar
sb.header("運用方針")
margin = sb.number_input("粗利率（%）", 1.0, 95.0, 30.0, 1.0,
                         help="販売価格から原価・送料・手数料を引いた利益の割合。目標ROASの基準になる最重要の設定です。"
                              "商品ごとに違う場合は商品マスタCSVで指定できます。")
mode = sb.radio("運用モード", list(MODES), index=1, horizontal=True,
                help="利益重視: 目標ROAS＝損益分岐×1.3／バランス: ×1.1／売上拡大: ×0.9（新規キーワードも積極的に追加）")
be = 100 / (margin / 100)
sb.caption(f"損益分岐ROAS {be:.0f}% → 目標ROAS **{be * MODES[mode]['mult']:.0f}%**")
existing_w = sb.slider("既存顧客の売上の評価", 0.0, 1.0, 1.0, 0.1,
                       help="既存顧客は広告が無くても購入した可能性があります。0.5にすると既存顧客の売上を半分として評価し、"
                            "新規顧客の獲得に強い入札になります（レポートに新規/既存の列がある場合のみ有効）。")
event = sb.toggle("セール期間（スーパーSALE・お買い物マラソン等）", value=False,
                  help="セール中は転換率が上がるため、成果の出ているキーワードの入札を上乗せします。終了後の運用で通常値に戻ります。")
event_boost = sb.slider("セール時の上乗せ（%）", 5, 50, 20, 5, disabled=not event) / 100 if event else 0.0

with sb.expander("詳細設定"):
    window = st.radio("売上の計測期間", ["720", "12"], index=0, horizontal=True, format_func=lambda x: f"{x}時間")
    min_kw = st.number_input("キーワードCPCの下限（円）", 1, 1000, 40)
    min_item = st.number_input("商品CPCの下限（円）", 1, 1000, 20)
    max_cpc = st.number_input("CPCの上限（円）", 10, 5000, 300, 10)
    change = st.slider("1回の最大変更幅（%）", 5, 50, 25, 5) / 100
    min_clicks = st.number_input("判定に必要なクリック数", 5, 500, 30, 5)
    kw_cap = st.number_input("1商品あたりのキーワード上限", 1, 50, 20,
                             help="2026年夏に10件→20件へ拡大されたとの情報があります。RMSで確認してください。")
    new_per = st.number_input("1回で追加する新規キーワード（1商品あたり）", 0, 20, MODES[mode]["new_kw_per_item"])
    new_max = st.number_input("新規キーワードのテストCPC上限（円）", 40, 1000, 80, 5)
    assumed_cvr = st.number_input("想定CVR（%）※RPP実績の無い商品の推定に使用", 0.1, 30.0, 1.0, 0.1,
                                  help="店舗のRPP実績のCVRと混ぜて使います。実績が増えるほど実績の値に近づきます。")
    del_ratio = st.slider("削除の基準（粗利1件分 × 倍率）", 0.5, 3.0, 1.0, 0.1,
                          help="受注0件のまま、この金額以上を使ったキーワードを削除します。")
    out_enc = st.radio("CSVの文字コード", ["cp932", "utf-8-sig"], horizontal=True,
                       format_func=lambda x: "Shift_JIS（RMS用）" if x == "cp932" else "UTF-8")

strategy = Strategy(
    default_margin=margin, mode=mode, existing_weight=existing_w, event_boost=event_boost,
    min_item_cpc=int(min_item), min_kw_cpc=int(min_kw), max_cpc=int(max_cpc), max_change_rate=change,
    min_clicks=int(min_clicks), max_kw_per_item=int(kw_cap), new_kw_per_item=int(new_per),
    new_kw_max_cpc=int(new_max), delete_ratio=del_ratio, assumed_cvr=float(assumed_cvr),
)

# ================================================================ データ投入
st.subheader("1. データを入れる")
st.markdown(
    "RMSのCSVを**まとめて**ドラッグしてください。種類は自動で判定します。"
    "　必須: **RPP キーワード別レポート**　推奨: **RPP 商品別レポート**（同じ期間）"
    "　**全商品をゼロから設計するなら: 商品一覧**（RMSの商品CSV・カテゴリCSVなど。販売価格があると精度が上がります）"
    "　任意: 検索キーワードデータ／商品マスタ（商品管理番号・粗利率）／前回の運用記録"
)
files = st.file_uploader("CSVファイル", type=["csv"], accept_multiple_files=True, label_visibility="collapsed")

loaded = []
for f in files or []:
    try:
        df = read_csv_bytes(f.getvalue())
        loaded.append({"name": f.name, "df": df, "kind": detect_kind(df)})
    except Exception as e:  # noqa: BLE001
        st.error(f"{f.name}: 読み込めませんでした（{e}）")

if loaded:
    kinds = list(LABELS)
    cols = st.columns(min(len(loaded), 4))
    for i, x in enumerate(loaded):
        with cols[i % 4]:
            x["kind"] = st.selectbox(x["name"], kinds, index=kinds.index(x["kind"]),
                                     format_func=lambda k: LABELS[k], key=f"kind_{x['name']}")
            if x["kind"] in ("item_report", "keyword_report"):
                d = report_period_days(x["df"])
                st.caption(f"{len(x['df'])}行・集計{d}日間" if d else f"{len(x['df'])}行")

if not loaded:
    with st.expander("📖 使い方・必要なデータ", expanded=True):
        st.markdown(
            """
**毎週1回、同じ曜日に**次の流れで運用します。

1. RMS → 広告 → RPP → パフォーマンスレポートで、集計期間を**直近30日**（大型セール期間は避ける）にして
   - 集計単位「**キーワード別**」
   - 集計単位「**商品別**」

   をCSVでダウンロード
2. 上の欄にまとめて入れる（前回このツールで保存した「運用記録CSV」も一緒に入れると、前回の施策の効果を自動で検証します）
3. 「アクションプラン」を確認し、「出力」タブのCSVをRMSの一括アップロードで反映
4. 「運用記録CSV」を保存（次回使います）

**このツールが自動で判断すること**
- 商品ごとの採算（損益分岐ROAS・必要CVR）と分類（拡大／維持／調整／構造的赤字／データ不足）
- 登録済みキーワードのCPC変更・削除
- 新しいキーワードの追加（商品名・他商品で売れた語・検索キーワードデータから候補を作り、採算が合うものだけテスト用CPCで追加）
- 商品CPC（キーワード以外の配信）の調整と除外候補
- 広告では解決できない商品のページ改善指示
"""
        )
    st.stop()

# ================================================================ 解析
def pick(kind):
    return [x["df"] for x in loaded if x["kind"] == kind]


try:
    item_raw = pick("item_report")
    kw_raw = pick("keyword_report")
    item_rep = first_or_none([prepare_report(d, "item", window) for d in item_raw])
    kw_rep = first_or_none([prepare_report(d, "keyword", window) for d in kw_raw])
    master = first_or_none([prepare_master(d) for d in pick("master")])
    search_kw = first_or_none([prepare_search_keywords(d) for d in pick("search_keywords")])
    history = first_or_none(pick("history"))
    catalog = first_or_none([prepare_catalog(d) for d in pick("catalog")])
    days = next((report_period_days(d) for d in kw_raw + item_raw if report_period_days(d)), None)
    if item_rep is None and kw_rep is None and catalog is None:
        st.warning("RPPの商品別・キーワード別レポート、または商品一覧を入れてください。")
        st.stop()
    plan = build_plan(item_rep, kw_rep, strategy, master=master, search_kw=search_kw, history=history,
                      period_days=days, catalog=catalog)
except ValueError as e:
    st.error(str(e))
    st.stop()

# ================================================================ 結果
st.subheader("2. 診断とアクションプラン")
tab_sum, tab_all, tab_items, tab_plan, tab_cand, tab_fb, tab_out = st.tabs(
    ["概要", "全商品の設計", "商品診断", "アクションプラン", "キーワード設計", "効果検証", "出力"]
)


def kpi(col, label, value):
    col.markdown(f'<div class="kpi"><div class="l">{label}</div><div class="v">{value}</div></div>',
                 unsafe_allow_html=True)


s = plan.summary
with tab_sum:
    c = st.columns(5)
    kpi(c[0], f"広告費（{int(s['集計日数']) if s['集計日数'] else '-'}日間）", f"{float(s['広告費'] or 0):,.0f}円")
    kpi(c[1], "広告経由の売上", f"{float(s['売上'] or 0):,.0f}円")
    kpi(c[2], "ROAS", "-" if pd.isna(s["ROAS(%)"]) else f"{s['ROAS(%)']:,.0f}%")
    kpi(c[3], "粗利 − 広告費", f"{s['粗利-広告費']:,.0f}円")
    kpi(c[4], "実行するアクション", f"{int(s['アクション数'])}件")
    if s.get("全商品数"):
        c2 = st.columns(4)
        kpi(c2[0], INV_ATTACK, f"{int(s[INV_ATTACK])}商品")
        kpi(c2[1], INV_TEST, f"{int(s[INV_TEST])}商品")
        kpi(c2[2], INV_DEFEND, f"{int(s[INV_DEFEND])}商品")
        kpi(c2[3], INV_STOP, f"{int(s[INV_STOP])}商品")

    st.markdown("#### 所見")
    items = plan.items
    insights = []
    target = float(items["目標ROAS(%)"].median()) if not items.empty else 100 / (margin / 100) * MODES[mode]["mult"]
    if s["粗利-広告費"] < 0:
        insights.append(f"期間中の広告は粗利ベースで <b>{-s['粗利-広告費']:,.0f}円の赤字</b> です"
                        f"（粗利率{margin:.0f}%で計算。目標ROASは{target:.0f}%）。")
    for _, it in items[items["分類"] == CLS_STRUCT].iterrows():
        insights.append(
            f"<b>{it['商品名'][:30]}…</b>：1クリックあたり売上 {it['1クリック売上']:.0f}円に対し、キーワードCPCの下限は{min_kw}円。"
            f"CVR {it['CVR(%)']}% を <b>{it['必要CVR(%)']}%以上</b> に上げない限り、入札調整だけでは黒字になりません。"
            "広告費を抑えつつ、商品ページの改善を優先してください。")
    if s["削除対象の広告費"] > 0:
        insights.append(f"受注につながっていないキーワードの削除で、期間中 <b>{s['削除対象の広告費']:,.0f}円</b> 相当の広告費を削減できます。")
    if s["新規キーワード数"] > 0:
        insights.append(f"新規キーワードを <b>{int(s['新規キーワード数'])}件</b> テスト追加します。次回の運用で成果を見て、継続・調整・削除を自動で判断します。")
    if s.get("全商品数") and s.get(INV_ATTACK, 0) == 0:
        need = min_kw * target / 100
        med_price = float(pd.to_numeric(plan.portfolio["価格"], errors="coerce").median() or 4000)
        insights.append(
            f"現在の数値では、キーワード広告で採算が合う商品がありません。キーワードCPCの下限{min_kw}円で目標ROAS{target:.0f}%を守るには、"
            f"<b>1クリックあたり{need:.0f}円以上の売上</b>（例: 価格{med_price:,.0f}円ならCVR {need / med_price * 100:.1f}%以上）が必要です。"
            "まず商品CPCの少額配信で商品ごとのCVRを測り、基準を超えた商品から順にキーワードを投入します（キーワード設計タブで準備済み）。")
    grow = items[items["分類"] == CLS_GROW] if not items.empty else items
    if not grow.empty:
        insights.append(f"目標を上回っている商品が{len(grow)}件あります。入札を上げて露出を広げます。")
    data_short = items[items["分類"] == CLS_DATA] if not items.empty else items
    if not data_short.empty:
        insights.append(f"クリックが{min_clicks}回未満でまだ判断できない商品が{len(data_short)}件あります。")
    for n in plan.notes:
        insights.append(n)
    for t in insights:
        st.markdown(f'<div class="insight">{t}</div>', unsafe_allow_html=True)

    st.markdown("#### 施策の内訳")
    cnt = plan.actions.groupby("施策")["実行"].agg(["count", "sum"]).rename(columns={"count": "件数", "sum": "うち自動実行"})
    st.dataframe(cnt, use_container_width=True)

with tab_all:
    if plan.portfolio.empty:
        st.info("商品一覧を入れると、RPPの実績が無い商品も含めて全商品の投資区分とキーワードを設計します。")
    else:
        st.caption("全商品を「1クリックあたりの売上（実績、または 価格×想定CVR）÷ 目標ROAS ＝ 採算CPC」で並べ、"
                   "キーワードに投資する商品・実績づくりから始める商品・商品CPCだけにする商品・停止を検討する商品に分けています。")
        inv = st.multiselect("投資区分", [INV_ATTACK, INV_TEST, INV_DEFEND, INV_STOP],
                             default=[INV_ATTACK, INV_TEST, INV_DEFEND, INV_STOP])
        pf = plan.portfolio[plan.portfolio["投資区分"].isin(inv)]
        st.dataframe(pf, use_container_width=True, hide_index=True,
                     column_config={"商品名": st.column_config.TextColumn(width="large"),
                                    "価格": st.column_config.NumberColumn(format="%d")})

with tab_items:
    if plan.items.empty:
        st.info("RPPのレポートを入れると、実績にもとづく商品診断を表示します。")
    order = {CLS_STRUCT: 0, CLS_TUNE: 1, CLS_GROW: 2, CLS_KEEP: 3, CLS_DATA: 4}
    view = plan.items.sort_values("分類", key=lambda x: x.map(order))
    cols = ["分類", "商品管理番号", "商品名", "クリック数", "実績額", "売上金額", "売上件数", "ROAS(%)", "見込みROAS(%)",
            "目標ROAS(%)", "CVR(%)", "必要CVR(%)", "客単価", "1クリック売上", "目標CPC", "粗利-広告費",
            "KW外クリック", "KW外ROAS(%)"]
    st.dataframe(view[[c for c in cols if c in view.columns]], use_container_width=True, hide_index=True)
    st.caption("見込みROAS: クリックの少ない商品を店舗平均に寄せて補正した値。必要CVR: キーワード下限CPCで目標ROASを満たすのに必要な転換率。"
               "KW外: 登録キーワード以外の検索から来たクリック（商品CPCで配信される部分）。")
    if not plan.keywords.empty:
        st.markdown("#### 登録済みキーワード")
        kcols = ["判定", "商品管理番号", "キーワード", "クリック数", "実績額", "売上金額", "売上件数", "見込みROAS(%)",
                 "目標ROAS(%)", "目安CPC", "CPC実績", "目標CPC", "根拠"]
        st.dataframe(plan.keywords[[c for c in kcols if c in plan.keywords.columns]], use_container_width=True,
                     hide_index=True)

with tab_plan:
    st.caption("「実行」のチェックを外すと出力されません。変更後CPCは直接編集できます。"
               "ページ改善・除外候補・受注のあるキーワードの削除は、人の判断が必要なため初期状態はチェックなしです。")
    kinds = st.multiselect("表示する施策", [KW_UPDATE, KW_NEW, KW_DELETE, ITEM_CPC, ITEM_EXCLUDE, PAGE_FIX],
                           default=[KW_UPDATE, KW_NEW, KW_DELETE, ITEM_CPC, ITEM_EXCLUDE, PAGE_FIX])
    shown = plan.actions[plan.actions["施策"].isin(kinds)]
    edited = st.data_editor(
        shown, use_container_width=True, hide_index=True, key="plan_editor",
        disabled=[c for c in shown.columns if c not in ("実行", "変更後CPC")],
        column_config={
            "実行": st.column_config.CheckboxColumn("実行"),
            "変更後CPC": st.column_config.NumberColumn("変更後CPC", min_value=1, step=1, format="%d"),
            "変更前CPC": st.column_config.NumberColumn(format="%d"),
            "根拠": st.column_config.TextColumn(width="large"),
        },
    )
    actions = plan.actions.copy()
    actions.loc[edited.index, ["実行", "変更後CPC"]] = edited[["実行", "変更後CPC"]]

with tab_cand:
    if plan.candidates.empty:
        st.info("新規キーワードの候補はありません。")
    else:
        st.caption("候補の作り方: ①検索キーワードデータ（実際の流入がある語）②他商品で売れた語の横展開 ③商品名の語の組み合わせ"
                   "（ブランド×固有の語／固有の語×カテゴリー／悩み・特徴×カテゴリー）。"
                   "採算CPC＝想定1クリック売上÷目標ROAS。「待機」の候補は、商品CPCでの実績づくりでCVRが確認できた時点で自動的に採用されます。")
        status = st.multiselect("判定", sorted(plan.candidates["判定"].str.split("（").str[0].unique()),
                                default=sorted(plan.candidates["判定"].str.split("（").str[0].unique()))
        q = st.text_input("商品名・キーワードで絞り込み", "")
        cv = plan.candidates[plan.candidates["判定"].str.split("（").str[0].isin(status)]
        if q:
            cv = cv[cv["商品名"].str.contains(q, case=False, na=False) | cv["キーワード"].str.contains(q, case=False, na=False)]
        st.dataframe(cv, use_container_width=True, hide_index=True)
        st.download_button("⬇️ キーワード設計（全候補）CSV", to_csv_bytes(plan.candidates, "utf-8-sig"),
                           "rpp_キーワード設計.csv", "text/csv")

with tab_fb:
    if plan.feedback.empty:
        st.info("前回の運用記録CSVを一緒に入れると、前回変更したキーワードの成果（クリック/日・ROASの変化）をここに表示し、"
                "下げすぎで露出が落ちたキーワードは自動で戻します。")
    else:
        st.dataframe(plan.feedback, use_container_width=True, hide_index=True)

with tab_out:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    kw_up = keyword_upload_from_actions(actions)
    item_cols = item_raw[0].columns if item_raw else None
    item_up = item_upload_from_actions(actions, item_cols)
    record = history_records(plan.__class__(**{**plan.__dict__, "actions": actions}), days)

    def csv_bytes(df, upload=False):
        return to_csv_bytes(df, out_enc, lineterminator="\n" if upload else "\r\n")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"##### キーワードCPC（{len(kw_up)}行）")
        n_cnt = (kw_up["コントロールカラム"] == "n").sum()
        u_cnt = (kw_up["コントロールカラム"] == "u").sum()
        d_cnt = (kw_up["コントロールカラム"] == "d").sum()
        st.caption(f"n 新規登録 {n_cnt}件 ／ u CPC変更 {u_cnt}件 ／ d 削除 {d_cnt}件")
        if len(kw_up):
            st.download_button("⬇️ キーワードCPC 一括アップロード用CSV", csv_bytes(kw_up, True),
                               f"rpp_keyword_upload_{stamp}.csv", "text/csv", use_container_width=True)
            st.dataframe(kw_up, use_container_width=True, hide_index=True, height=240)
    with c2:
        st.markdown(f"##### 商品CPC（{len(item_up)}行）")
        if not item_raw:
            st.caption("商品別レポートを入れると、商品CPCの調整も出力します。")
        elif len(item_up):
            st.caption("⚠️ 商品CPCの一括アップロード形式は、RMSのサンプルでまだ確認できていません。"
                       "エラーが出た場合は、RMSの「商品CPCの登録/更新」のサンプルCSVを送ってください。")
            st.download_button("⬇️ 商品CPC 一括アップロード用CSV", csv_bytes(item_up, True),
                               f"rpp_item_upload_{stamp}.csv", "text/csv", use_container_width=True)
            st.dataframe(item_up, use_container_width=True, hide_index=True, height=240)

    st.markdown("##### 運用記録（次回、レポートと一緒に読み込んでください）")
    st.download_button("⬇️ 運用記録CSV", to_csv_bytes(record, "utf-8-sig"), f"rpp_運用記録_{stamp}.csv", "text/csv")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if len(kw_up):
            z.writestr(f"rpp_keyword_upload_{stamp}.csv", csv_bytes(kw_up, True))
        if len(item_up):
            z.writestr(f"rpp_item_upload_{stamp}.csv", csv_bytes(item_up, True))
        z.writestr(f"rpp_運用記録_{stamp}.csv", to_csv_bytes(record, "utf-8-sig"))
        z.writestr(f"rpp_アクションプラン_{stamp}.csv", to_csv_bytes(actions, "utf-8-sig"))
        if not plan.items.empty:
            z.writestr(f"rpp_商品診断_{stamp}.csv", to_csv_bytes(plan.items.drop(columns=["_source"], errors="ignore"), "utf-8-sig"))
        if not plan.portfolio.empty:
            z.writestr(f"rpp_全商品の設計_{stamp}.csv", to_csv_bytes(plan.portfolio, "utf-8-sig"))
        if not plan.candidates.empty:
            z.writestr(f"rpp_キーワード候補_{stamp}.csv", to_csv_bytes(plan.candidates, "utf-8-sig"))
    st.download_button("📦 すべてまとめてダウンロード（ZIP）", buf.getvalue(), f"rpp_運用_{stamp}.zip",
                       "application/zip", type="primary")

    st.markdown(
        """
**RMSへの反映**
1. RMS → 広告 → RPP →「商品・キーワード設定」→「一括アップロード」
2. キーワードは「**キーワードCPCの登録/更新**」、商品は「**商品CPCの登録/更新**」を選び、上のCSVをそのままアップロード
3. エラー結果CSVが返ってきた場合は、内容を確認（よくある例：「既に登録済」→ そのキーワードの n を u に変更）

⚠️ アップロード用CSVは **Excelで開いて上書き保存しない** でください。商品管理番号が「4.57E+12」の形に変わり、正しく登録できなくなります。
"""
    )

st.caption("RPP自動運用ツール v0.4 | SUNLINE合同会社")
