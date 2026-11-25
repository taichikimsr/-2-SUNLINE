"""通知済み商品の記録（同じ商品を何度も通知しないため）"""

from __future__ import annotations

import json
import os
import time

KEEP_DAYS = 60


class SeenStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f) or {}
            except (OSError, ValueError):
                self.data = {}

    def should_alert(self, item_id: str, price: int) -> tuple[bool, int | None]:
        """(通知すべきか, 値下げ前の価格) を返す。
        未通知なら通知、通知済みでも前回通知時より値下がりしていれば再通知する。"""
        rec = self.data.get(item_id)
        if rec is None:
            return True, None
        prev = rec.get("alerted_price")
        if prev is not None and price < prev:
            return True, prev
        return False, None

    def mark(self, item_id: str, price: int, watch: str) -> None:
        self.data[item_id] = {
            "alerted_price": price,
            "watch": watch,
            "ts": int(time.time()),
        }

    def prune(self) -> None:
        limit = time.time() - KEEP_DAYS * 86400
        self.data = {k: v for k, v in self.data.items() if v.get("ts", 0) >= limit}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
