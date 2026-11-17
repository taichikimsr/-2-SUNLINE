import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rpp.bidding import (  # noqa: E402
    ACTION_DOWN, ACTION_HOLD, ACTION_KEEP, ACTION_REVIEW, ACTION_UP,
    ColumnMap, Settings, attach_master, item_vpc_map, normalize_report, recommend,
)
from rpp.export import build_upload  # noqa: E402
from rpp.io import detect_columns, read_csv_bytes, to_csv_bytes, to_number  # noqa: E402

SAMPLE = os.path.join(ROOT, "sample_data")


def _read(name):
    with open(os.path.join(SAMPLE, name), "rb") as f:
        return read_csv_bytes(f.read())


def _item_result(settings=None, master=None):
    df = _read("rpp_item_report_sample.csv")
    cmap = ColumnMap.from_dict(detect_columns(df.columns, prefer_window="720"))
    rep = attach_master(normalize_report(df, cmap, "item"), master)
    return recommend(rep, settings or Settings(), "item")


def test_read_cp932_with_preamble_and_detect_columns():
    df = _read("rpp_item_report_sample.csv")
    assert df.columns[0] == "商品管理番号"
    det = detect_columns(df.columns, prefer_window="720")
    assert det["sales"] == "売上金額(合計720時間)"
    assert det["orders"] == "売上件数(合計720時間)"
    assert det["current_cpc"] == "商品CPC"
    assert det["actual_cpc"] == "CPC実績(合計)"
    det12 = detect_columns(df.columns, prefer_window="12")
    assert det12["sales"] == "売上金額(合計12時間)"


def test_to_number():
    s = to_number(pd.Series(["1,234円", "12.5%", "-", "", "１００"]))
    assert s.iloc[0] == 1234 and s.iloc[1] == 12.5 and pd.isna(s.iloc[2]) and pd.isna(s.iloc[3])
    assert s.iloc[4] == 100


def test_item_actions():
    r = _item_result().set_index("商品管理番号")
    # A: ROAS 833% > 400% → 上限+20%（60→72）
    assert r.loc["sl-001", "判定"] == ACTION_UP
    assert r.loc["sl-001", "推奨CPC"] == 72
    # B: ROAS 200% < 400% → 引き下げ（-20%まで）
    assert r.loc["sl-002", "判定"] == ACTION_DOWN
    assert r.loc["sl-002", "推奨CPC"] == 48
    # C: 売上0・広告費5,400円 ≥ 3,000円 → 引き下げ
    assert r.loc["sl-003", "判定"] == ACTION_DOWN
    assert r.loc["sl-003", "推奨CPC"] == 48
    # D: クリック12回 → データ不足
    assert r.loc["sl-004", "判定"] == ACTION_HOLD
    assert r.loc["sl-004", "推奨CPC"] == 30
    # E: 実績ROAS 400%だが、店舗平均に寄せた補正で見込みROASが上がり小幅引き上げ（40→43）
    assert r.loc["sl-005", "判定"] == ACTION_UP
    assert r.loc["sl-005", "推奨CPC"] == 43
    # 平滑化なしなら目標ちょうどで据え置き
    r0 = _item_result(Settings(prior_clicks=0)).set_index("商品管理番号")
    assert r0.loc["sl-005", "判定"] == ACTION_KEEP


def test_bounds_respected():
    s = Settings(max_change_rate=1.0, max_cpc=65, min_item_cpc=20)
    r = _item_result(s)
    assert r["推奨CPC"].max() <= 65
    assert r["推奨CPC"].min() >= 20


def test_review_when_floor_cannot_meet_target():
    s = Settings(target_roas_pct=2000, max_change_rate=1.0)
    r = _item_result(s).set_index("商品管理番号")
    assert r.loc["sl-002", "推奨CPC"] == 20
    assert r.loc["sl-002", "判定"] == ACTION_REVIEW


def test_margin_mode_and_individual_target():
    master = pd.DataFrame({"商品管理番号": ["sl-002", "sl-005"], "粗利率": [25.0, 45.0], "目標ROAS": [None, 500.0]})
    s = Settings(margin_mode=True, margin_safety=1.0)
    r = _item_result(s, master).set_index("商品管理番号")
    assert r.loc["sl-002", "目標ROAS(%)"] == 400.0  # 100 / 0.25
    assert r.loc["sl-005", "目標ROAS(%)"] == 500.0  # 個別目標が優先


def test_keyword_uses_item_prior_and_min():
    item = _item_result()
    df = _read("rpp_keyword_report_sample.csv")
    cmap = ColumnMap.from_dict(detect_columns(df.columns, prefer_window="720"))
    det = detect_columns(df.columns)
    assert det["current_cpc"] == "キーワードCPC"
    rep = normalize_report(df, cmap, "keyword")
    r = recommend(attach_master(rep, None), Settings(), "keyword", prior_vpc=item_vpc_map(item))
    r = r.set_index(["商品管理番号", "キーワード"])
    assert r.loc[("sl-001", "キーワードA1"), "判定"] == ACTION_UP
    assert r.loc[("sl-001", "キーワードA2"), "判定"] == ACTION_DOWN  # 売上0・4,800円
    assert r.loc[("sl-005", "キーワードE1"), "判定"] == ACTION_HOLD
    assert r["推奨CPC"].min() >= 40


def test_aggregate_daily_rows():
    df = pd.DataFrame({
        "商品管理番号": ["a", "a", "合計"],
        "クリック数": ["10", "30", "40"],
        "実績額": ["500", "1,500", "2,000"],
        "売上金額": ["0", "10,000", "10,000"],
    })
    cmap = ColumnMap(item_id="商品管理番号", clicks="クリック数", spend="実績額", sales="売上金額")
    rep = normalize_report(df, cmap, "item")
    assert len(rep) == 1
    assert rep.iloc[0]["クリック数"] == 40 and rep.iloc[0]["実績額"] == 2000
    assert rep.iloc[0]["CPC実績"] == 50


def test_missing_required_column():
    with pytest.raises(ValueError):
        normalize_report(pd.DataFrame({"x": [1]}), ColumnMap(), "item")


def test_build_upload_simple_and_template():
    r = _item_result()
    up = build_upload(r, "item")
    assert list(up.columns) == ["商品管理番号", "CPC"]
    assert set(up["商品管理番号"]) == {"sl-001", "sl-002", "sl-003", "sl-005"}

    tmpl = pd.DataFrame({"商品管理番号": ["sl-001"], "商品CPC": ["60"], "備考": ["既存"]})
    up2 = build_upload(r, "item", tmpl)
    assert list(up2.columns) == ["商品管理番号", "商品CPC", "備考"]
    row = up2.set_index("商品管理番号").loc["sl-001"]
    assert row["商品CPC"] == 72 and row["備考"] == "既存"

    with pytest.raises(ValueError):
        build_upload(r, "item", pd.DataFrame({"foo": []}))


def test_csv_encoding_error_message():
    df = pd.DataFrame({"a": ["\U0001F600"]})
    with pytest.raises(ValueError):
        to_csv_bytes(df, "cp932")
    assert to_csv_bytes(pd.DataFrame({"a": ["テスト"]}), "cp932").decode("cp932").startswith("a\r\n")


def test_read_rms_style_preamble():
    # 説明行に「商品管理番号」を含む1列の行があっても、本当のヘッダー行を見つける
    text = (
        "楽天RPPレポート\r\n"
        "期間：2026/10/18～2026/11/16\r\n"
        "検索条件：商品管理番号 指定なし\r\n"
        "\r\n"
        "日付,商品管理番号,商品名,クリック数(合計),実績額(合計),売上金額(合計720時間),売上件数(合計720時間)\r\n"
        "2026/11/01,a-1,商品A,10,500,3000,1\r\n"
        "2026/11/02,a-1,商品A,20,1000,0,0\r\n"
        "合計,,,30,1500,3000,1,余分な列\r\n"
    )
    df = read_csv_bytes(text.encode("cp932"))
    assert df.columns[1] == "商品管理番号"
    assert len(df) == 3
    cmap = ColumnMap.from_dict(detect_columns(df.columns, prefer_window="720"))
    rep = normalize_report(df, cmap, "item")
    assert len(rep) == 1 and rep.iloc[0]["クリック数"] == 30


def test_read_duplicate_headers():
    df = read_csv_bytes("商品管理番号,CPC,CPC\na,1,2\n".encode("utf-8"))
    assert list(df.columns) == ["商品管理番号", "CPC", "CPC_1"]
