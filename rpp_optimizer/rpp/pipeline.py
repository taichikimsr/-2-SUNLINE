"""画面を使わずに運用を1回まわす（自動運用・定期実行から呼ぶ）

入力フォルダのCSVをすべて読み込み（種類は自動判定）、アクションプランを作り、
出力フォルダに RMS一括アップロード用CSV と 運用記録・各種レポート を保存する。
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from .datasets import first_or_none, prepare_catalog, prepare_master, prepare_report, prepare_search_keywords
from .export import exclusion_upload_from_actions, item_upload_from_actions, keyword_upload_from_actions
from .io import classify_dataset, read_csv_bytes, report_period_days, to_csv_bytes
from .settings import to_strategy
from .strategy import build_plan, history_records


def run_once(input_dir: str, out_dir: str, settings: Dict[str, Any], history_dir: Optional[str] = None) -> Dict[str, Any]:
    files = sorted(glob.glob(os.path.join(input_dir, "*.csv")))
    if history_dir:
        # 直近の運用記録を自動で読み込む（前回の施策の効果検証）
        hist = sorted(glob.glob(os.path.join(history_dir, "**", "rpp_運用記録_*.csv"), recursive=True))
        if hist:
            files.append(hist[-1])
    loaded = []
    for f in files:
        with open(f, "rb") as fh:
            df = read_csv_bytes(fh.read())
        loaded.append((os.path.basename(f), classify_dataset(df), df))

    def pick(kind):
        return [df for _, k, df in loaded if k == kind]

    window = str(settings.get("window", "720"))
    item_raw, kw_raw = pick("item_report"), pick("keyword_report")
    item_rep = first_or_none([prepare_report(d, "item", window) for d in item_raw])
    kw_rep = first_or_none([prepare_report(d, "keyword", window) for d in kw_raw])
    days = next((report_period_days(d) for d in kw_raw + item_raw if report_period_days(d)), None)
    plan = build_plan(
        item_rep, kw_rep, to_strategy(settings),
        master=first_or_none([prepare_master(d) for d in pick("master")]),
        search_kw=first_or_none([prepare_search_keywords(d) for d in pick("search_keywords")]),
        history=first_or_none(pick("history")),
        period_days=days,
        catalog=first_or_none([prepare_catalog(d) for d in pick("catalog")]),
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    run_dir = os.path.join(out_dir, stamp)
    os.makedirs(run_dir, exist_ok=True)
    enc = settings.get("out_encoding", "cp932")
    outputs = {}

    def write(name, df, upload=False):
        if df is None or len(df) == 0:
            return
        path = os.path.join(run_dir, name)
        data = to_csv_bytes(df, enc, "\n") if upload else to_csv_bytes(df, "utf-8-sig")
        with open(path, "wb") as fh:
            fh.write(data)
        outputs[name] = path

    write(f"rpp_keyword_upload_{stamp}.csv", keyword_upload_from_actions(plan.actions), True)
    write(f"rpp_item_upload_{stamp}.csv",
          item_upload_from_actions(plan.actions, item_raw[0].columns if item_raw else None), True)
    write(f"rpp_exclude_upload_{stamp}.csv", exclusion_upload_from_actions(plan.actions), True)
    write(f"rpp_運用記録_{stamp}.csv", history_records(plan, days))
    write(f"rpp_アクションプラン_{stamp}.csv", plan.actions)
    write(f"rpp_広告適性_{stamp}.csv", plan.portfolio)
    write(f"rpp_予算配分_{stamp}.csv", plan.budget)
    write(f"rpp_キーワード設計_{stamp}.csv", plan.candidates)

    summary = {k: (None if v != v else v) for k, v in plan.summary.items()}  # NaN → None
    result = {
        "実行日時": stamp,
        "入力ファイル": [{"ファイル": n, "種類": k} for n, k, _ in loaded],
        "概要": summary,
        "所見": plan.notes,
        "出力": outputs,
    }
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
    return result
