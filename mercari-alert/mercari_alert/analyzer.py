"""相場算出とお得判定"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .client import Item

# 本体以外（箱・付属品・パーツ）や偽物・故障品を示しやすい語。
# 商品名にこれらが含まれる出品は、相場算出にも通知にも使わない。
# ※「外箱あり」「故障なし」「ショップ袋付き」のように本物の出品でも使われる語は
#   誤って除外しないよう、あえて入れていない。
DEFAULT_EXCLUDE_WORDS = [
    "箱のみ", "空箱", "保証書のみ", "ギャランティのみ", "冊子のみ", "付属品のみ",
    "ベルトのみ", "バンドのみ", "ブレスのみ", "コマのみ", "余りコマ", "替えベルト",
    "社外品", "互換", "レプリカ", "コピー品", "スーパーコピー", "N級", "オマージュ",
    "ジャンク", "部品取り", "動作不良", "不動品", "ノベルティ",
    "ケースのみ", "保存袋のみ", "ショッパーのみ",
]

CONDITION_NAMES = {
    1: "新品、未使用",
    2: "未使用に近い",
    3: "目立った傷や汚れなし",
    4: "やや傷や汚れあり",
    5: "傷や汚れあり",
    6: "全体的に状態が悪い",
}


@dataclass
class Market:
    price: int | None          # 判定に使う相場（円）
    source: str                # "reference" / "sold_median" / "none"
    sold_median: int | None
    sold_samples: int
    on_sale_median: int | None
    on_sale_samples: int


@dataclass
class Deal:
    item: Item
    market: Market
    discount: float            # 相場からの値引率（0.30 = 30%安い）
    suspicious: bool           # 安すぎる（偽物・付属品・誤記の疑い）
    price_drop_from: int | None = None  # 値下げで再通知する場合の前回価格


def name_excluded(name: str, words: list[str]) -> bool:
    n = name.lower()
    return any(w and w.lower() in n for w in words)


def filter_items(
    items: list[Item],
    exclude_words: list[str],
    price_min: int | None = None,
    price_max: int | None = None,
    condition_ids: list[int] | None = None,
    include_shops: bool = True,
    include_auctions: bool = True,
    required_words: list[str] | None = None,
) -> list[Item]:
    """ローカル側の絞り込み（API側の絞り込みが効かなかった場合の保険も兼ねる）"""
    out = []
    for it in items:
        if price_min is not None and it.price < price_min:
            continue
        if price_max is not None and it.price > price_max:
            continue
        if condition_ids and it.condition_id is not None and it.condition_id not in condition_ids:
            continue
        if not include_shops and it.is_shop:
            continue
        if not include_auctions and it.is_auction:
            continue
        if name_excluded(it.name, exclude_words):
            continue
        if required_words:
            n = it.name.lower()
            if not all(w.lower() in n for w in required_words):
                continue
        out.append(it)
    return out


def _median(prices: list[int]) -> int | None:
    return int(statistics.median(prices)) if prices else None


def compute_market(
    sold: list[Item],
    on_sale: list[Item],
    reference_price: int | None,
    min_sold_samples: int,
) -> Market:
    sold_prices = [i.price for i in sold]
    on_sale_prices = [i.price for i in on_sale]
    sold_med = _median(sold_prices)
    on_sale_med = _median(on_sale_prices)

    if reference_price:
        price, source = int(reference_price), "reference"
    elif sold_med is not None and len(sold_prices) >= min_sold_samples:
        price, source = sold_med, "sold_median"
    else:
        price, source = None, "none"
    return Market(
        price=price,
        source=source,
        sold_median=sold_med,
        sold_samples=len(sold_prices),
        on_sale_median=on_sale_med,
        on_sale_samples=len(on_sale_prices),
    )


def find_deals(
    on_sale: list[Item],
    market: Market,
    discount_threshold: float,
    suspicious_ratio: float,
) -> list[Deal]:
    """相場より discount_threshold 以上安い出品を、安い順（値引率の大きい順）に返す"""
    if not market.price:
        return []
    deals = []
    limit = market.price * (1 - discount_threshold)
    for it in on_sale:
        if it.price <= limit:
            discount = 1 - it.price / market.price
            suspicious = it.price < market.price * suspicious_ratio
            deals.append(Deal(it, market, discount, suspicious))
    deals.sort(key=lambda d: d.discount, reverse=True)
    return deals
