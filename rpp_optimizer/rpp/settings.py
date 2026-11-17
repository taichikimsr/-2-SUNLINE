"""運用設定の保存・読み込み（画面・コマンド・自動運用で同じ設定を使う）

保存先: rpp_optimizer/settings.json（このPCだけに保存。GitHubには上げない）
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from typing import Any, Dict

from .strategy import Strategy

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(BASE, "settings.json")

# Strategy 以外の設定（画面・出力用）
EXTRA_DEFAULTS: Dict[str, Any] = {
    "window": "720",          # 売上の計測期間（720時間／12時間）
    "out_encoding": "cp932",  # アップロード用CSVの文字コード
    "auto_apply": False,      # 自動運用で、RMSへの反映まで行うか（False: CSV作成まで）
}


def defaults() -> Dict[str, Any]:
    d = asdict(Strategy())
    d.update(EXTRA_DEFAULTS)
    return d


def load(path: str = SETTINGS_PATH) -> Dict[str, Any]:
    d = defaults()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d.update({k: v for k, v in json.load(f).items() if k in d})
        except (OSError, ValueError):
            pass  # 壊れていれば初期値で動かす
    return d


def save(values: Dict[str, Any], path: str = SETTINGS_PATH) -> None:
    d = load(path)
    d.update({k: v for k, v in values.items() if k in d})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def to_strategy(values: Dict[str, Any]) -> Strategy:
    names = {f.name for f in fields(Strategy)}
    return Strategy(**{k: v for k, v in values.items() if k in names})
