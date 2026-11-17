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
