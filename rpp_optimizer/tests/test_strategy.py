import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rpp.datasets import prepare_report, prepare_search_keywords  # noqa: E402
from rpp.export import item_upload_from_actions, keyword_upload_from_actions  # noqa: E402
from rpp.io import classify_dataset, read_csv_bytes, report_period_days  # noqa: E402
from rpp.keywords import normalize_keyword, title_candidates, token_document_frequency, tokenize  # noqa: E402
from rpp.strategy import (  # noqa: E402
    CLS_GROW, CLS_STRUCT, ITEM_CPC, KW_DELETE, KW_NEW, KW_UPDATE, PAGE_FIX, Strategy, build_plan, history_records,
)

S = os.path.join(ROOT, "sample_data")


def _read(n):
    with open(os.path.join(S, n), "rb") as f:
        return read_csv_bytes(f.read())


def _plan(**kw):
    item = _read("rms_item_report_sample.csv")
    kwr = _read("rms_keyword_report_sample.csv")
    sk = _read("search_keywords_sample.csv")
    s = Strategy(default_margin=40, **kw)
    return build_plan(prepare_report(item, "item"), prepare_report(kwr, "keyword"), s,
                      search_kw=prepare_search_keywords(sk), period_days=report_period_days(kwr))


def test_classify_and_period():
    assert classify_dataset(_read("rms_keyword_report_sample.csv")) == "keyword_report"
    assert classify_dataset(_read("rms_item_report_sample.csv")) == "item_report"
    assert classify_dataset(_read("search_keywords_sample.csv")) == "search_keywords"
    assert classify_dataset(_read("item_master_sample.csv")) == "master"
    assert report_period_days(_read("rms_keyword_report_sample.csv")) == 30


def test_tokenize_and_candidates():
    t = tokenize("【正規品】ブランドX スパイダーマン プラモデル 1/12 可動 フィギュア プレゼント 男の子")
    assert t == ["ブランドX", "スパイダーマン", "プラモデル", "フィギュア"]
    df = token_document_frequency(["ブランドX スパイダーマン フィギュア"])
    kws = [k for k, _, _ in title_candidates("ブランドX スパイダーマン フィギュア", df, 1, extra_anchors={"ブランドx"})]
    assert "ブランドX スパイダーマン" in kws
    assert "スパイダーマン フィギュア" in kws  # カテゴリー語は後ろ
    assert normalize_keyword("フィギュア  スパイダーマン") == normalize_keyword("スパイダーマン　フィギュア")


def test_item_classes():
    items = _plan().items.set_index("商品管理番号")
    assert items.loc["smp-002", "分類"] == CLS_STRUCT  # CVR 0.64%
    assert items.loc["smp-004", "分類"] == CLS_GROW
    # キーワード以外の配信を分けて評価している
    assert items.loc["smp-001", "KW外クリック"] == 300


def test_actions():
    a = _plan().actions
    # 受注0で粗利1件分以上を使ったキーワードは削除
    d = a[(a["施策"] == KW_DELETE) & a["実行"]]
    assert set(d["キーワード"]) == {"フィギュア 可動", "怪獣 おもちゃ"}
    # 好調キーワードは引き上げ
    up = a[(a["施策"] == KW_UPDATE) & (a["キーワード"] == "宇宙戦艦 プラモデル")].iloc[0]
    assert up["変更後CPC"] > up["変更前CPC"]
    # 構造的赤字の商品にはページ改善、新規キーワードは追加しない
    assert ((a["施策"] == PAGE_FIX) & (a["商品管理番号"] == "smp-002")).any()
    assert not ((a["施策"] == KW_NEW) & (a["商品管理番号"] == "smp-002")).any()
    # 検索キーワードデータの語が追加される
    new = a[a["施策"] == KW_NEW]
    assert "星の騎士" in set(new["キーワード"])
    assert (new["変更後CPC"] >= 40).all()
    # 登録済みキーワードは新規に入らない
    assert "星の騎士 フィギュア" not in set(new["キーワード"])
    # 商品CPC
    assert ((a["施策"] == ITEM_CPC) & (a["商品管理番号"] == "smp-002")).any()


def test_upload_files():
    p = _plan()
    up = keyword_upload_from_actions(p.actions)
    assert list(up.columns) == ["コントロールカラム", "商品管理番号", "キーワード", "キーワードCPC"]
    assert set(up["コントロールカラム"]) <= {"n", "u", "d"}
    assert (up.loc[up["コントロールカラム"] == "d", "キーワードCPC"] != "").all()
    item = item_upload_from_actions(p.actions, _read("rms_item_report_sample.csv").columns)
    assert list(item.columns) == ["コントロールカラム", "商品管理番号", "商品CPC"]
    assert (item["コントロールカラム"] == "u").all()


def test_event_boost_and_modes():
    def cpc(p):
        a = p.actions
        return a[(a["施策"] == KW_UPDATE) & (a["キーワード"] == "宇宙戦艦 プラモデル")]["変更後CPC"].iloc[0]
    assert cpc(_plan(event_boost=0.2)) > cpc(_plan())
    assert len(_plan(mode="売上拡大").actions) >= len(_plan(mode="利益重視").actions)


def test_feedback_restores_over_cut():
    p = _plan()
    rec = history_records(p, 30)
    # 前回 引き下げたことにし、当時は1日10クリックあった → 今回は大幅減
    row = {"運用記録ID": "x", "施策": KW_UPDATE, "商品管理番号": "smp-004", "キーワード": "宇宙戦艦 プラモデル",
           "変更前CPC": 80, "変更後CPC": 60, "集計日数": 30, "クリック数": 300, "実績額": 18000,
           "売上金額": 80000, "ROAS(%)": 444}
    hist = pd.concat([rec, pd.DataFrame([row])], ignore_index=True)
    item = _read("rms_item_report_sample.csv")
    kwr = _read("rms_keyword_report_sample.csv")
    p2 = build_plan(prepare_report(item, "item"), prepare_report(kwr, "keyword"), Strategy(default_margin=40),
                    history=hist, period_days=30)
    act = p2.actions
    a = act[(act["施策"] == KW_UPDATE) & (act["キーワード"] == "宇宙戦艦 プラモデル")].iloc[0]
    assert "半減" in a["根拠"]
    assert not p2.feedback.empty


def test_real_rms_header_without_item_report():
    kwr = _read("rms_keyword_report_sample.csv")
    p = build_plan(None, prepare_report(kwr, "keyword"), Strategy(default_margin=40), period_days=30)
    assert any("商品別レポートが無い" in n for n in p.notes)
    assert not (p.actions["施策"] == ITEM_CPC).any()


def _catalog():
    from rpp.datasets import prepare_catalog
    text = (
        "コントロールカラム,商品管理番号（商品URL）,商品名,表示先カテゴリ,優先度\r\n"
        '"","smp-001","【正規品】サンプルブランド 星の騎士 フィギュア 可動 プラモデル","BLOKEES ブロッキーズ","1"\r\n'
        '"","new-01","DHC 薬用リップクリーム 保湿 リップクリーム 無香料","コスメ・ビューティー\\\\リップケア\\\\リップクリーム","1"\r\n'
        '"","new-01","DHC 薬用リップクリーム 保湿 リップクリーム 無香料","『500円均一』セレクション","1"\r\n'
        '"","new-02","エクササイズバンド トレーニングチューブ 筋トレ 美尻","アウトドア・レジャー\\\\筋トレ","1"\r\n'
    )
    df = read_csv_bytes(text.encode("cp932"))
    assert classify_dataset(df) == "catalog"
    return prepare_catalog(df)


def test_catalog_zero_base():
    cat = _catalog()
    assert len(cat) == 3  # 同じ商品の重複行はまとめる
    assert cat.set_index("商品管理番号").loc["new-01", "価格の目安"] == 500
    item = _read("rms_item_report_sample.csv")
    kwr = _read("rms_keyword_report_sample.csv")
    p = build_plan(prepare_report(item, "item"), prepare_report(kwr, "keyword"),
                   Strategy(default_margin=40, assumed_cvr=3), catalog=cat, period_days=30)
    pf = p.portfolio.set_index("商品管理番号")
    assert {"new-01", "new-02", "smp-001"} <= set(pf.index)
    assert pf.loc["new-01", "価格の出所"] == "店舗カテゴリの価格"
    # 500円の商品はキーワード下限では採算が合わない → テスト（実績づくり）
    from rpp.strategy import INV_TEST
    assert pf.loc["new-01", "投資区分"] == INV_TEST
    c = p.candidates
    kws = set(c[c["商品管理番号"] == "new-01"]["キーワード"])
    assert "DHC 薬用リップクリーム" in kws or "DHC リップクリーム" in kws
    assert any("保湿" in k and "リップクリーム" in k for k in kws)  # 悩み・特徴×カテゴリー
    kws2 = set(c[c["商品管理番号"] == "new-02"]["キーワード"])
    assert "エクササイズバンド" in kws2  # ブランドの無い商品は商品の種類そのもの
    # 実績の無い商品は商品CPCのテスト配信（n）
    a = p.actions
    t = a[(a["施策"] == ITEM_CPC) & (a["商品管理番号"] == "new-01")].iloc[0]
    assert t["コントロール"] == "n" and t["変更後CPC"] >= 20


def test_catalog_only():
    p = build_plan(None, None, Strategy(default_margin=40), catalog=_catalog())
    assert len(p.portfolio) == 3
    assert any("レポートが無い" in n for n in p.notes)


def test_keyword_quality_rules():
    from rpp.keywords import script_aliases
    names = ["Attenir アテニア ローション 化粧水", "Attenir アテニア リップオイル", "アテニア Attenir 乳液"]
    al = script_aliases(names)
    assert frozenset(("attenir", "アテニア")) in al
    df = token_document_frequency(names)
    kws = [k for k, _, _ in title_candidates("【新店Open 特別価格】資生堂 エリクシール 81146GV06 9歳以上 化粧水 乾燥肌",
                                              df, 3, extra_anchors={"資生堂"})]
    assert not any("特別価格" in k or "81146" in k or "9歳" in k for k in kws)
    assert "乾燥肌 化粧水" in kws
