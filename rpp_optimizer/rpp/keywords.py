"""キーワードの正規化・商品名からの語の抽出・新規キーワード候補の生成

RPPのレポートには「登録済みのキーワード」しか載らないため、新しいキーワードは次の3つの材料から探す。

1. 検索キーワードデータ（RMSのアクセス分析などから出力した、実際に検索・流入があった語）
   → 需要が実在する語。最優先で使う。
2. 他の商品で売れているキーワード（横展開）
   → 同じ語を商品名に含む別商品にも登録する。
3. 商品名（タイトル）から作る組み合わせ
   → 楽天の商品名は検索を意識して語が並べられているため、そこから
     「固有の語（キャラクター・シリーズ・型番など）」×「ブランド・カテゴリー語」の2語の組み合わせを作る。
     1語だけの広いキーワード（例:「フィギュア」）は、クリック単価が高く転換率が低くなりやすいため作らない。

いずれも「需要がありそうな仮説」であり、検索数が保証されたものではない。
そのため新規キーワードはテスト用の控えめなCPCで登録し、次回の運用で成果を見て
継続・入札調整・削除を自動で判断する（テスト→検証のサイクル）。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from itertools import combinations
from typing import Dict, Iterable, List, Set, Tuple

# 検索語としての価値が低い語（宣伝文句・汎用的な修飾語など）
STOP_WORDS: Set[str] = {
    "正規品", "公式", "公式ストア", "送料無料", "送料込", "送料込み", "あす楽", "即納", "即日発送", "翌日発送",
    "在庫あり", "予約", "新品", "新作", "新商品", "限定", "限定版", "数量限定", "期間限定", "人気", "大人気",
    "おすすめ", "オススメ", "話題", "ランキング", "ランキング1位", "1位", "ポイント", "ポイント10倍", "倍", "円",
    "off", "sale", "セール", "クーポン", "割引", "特価", "激安", "最安", "安い", "お得", "まとめ買い",
    "プレゼント", "ギフト", "贈り物", "誕生日", "誕生日プレゼント", "クリスマス", "クリスマスプレゼント",
    "母の日", "父の日", "敬老の日", "バレンタイン", "ホワイトデー", "お祝い", "記念日", "ラッピング",
    "男の子", "女の子", "子供", "子ども", "こども", "キッズ", "大人", "メンズ", "レディース", "ユニセックス",
    "小学生", "中学生", "高校生", "かわいい", "可愛い", "かっこいい", "おしゃれ", "オシャレ",
    "塗装不要", "接着剤不要", "組み立て", "簡単", "便利", "国内発送", "日本製", "海外", "輸入",
    "対象", "対応", "用", "セット", "個", "本", "枚", "点", "種", "種類", "タイプ", "サイズ", "カラー",
    "キャラクター", "アクション", "可動", "祝い", "お祝い", "ブランド", "シリーズ", "モデル", "アイテム",
    "and", "the", "for", "with", "of", "a", "an",
}

# ブランド・シリーズと組み合わせると購買意図が明確になる「カテゴリー語」（店舗共通で使える一般語）
CATEGORY_WORDS: Set[str] = {
    "フィギュア", "アクションフィギュア", "プラモデル", "プラモ", "模型", "おもちゃ", "玩具", "ブロック",
    "ミニカー", "ぬいぐるみ", "人形", "ドール", "グッズ", "コレクション", "トレカ", "カード", "ゲーム",
    "tシャツ", "パーカー", "トレーナー", "ジャケット", "コート", "パンツ", "スカート", "ワンピース", "シャツ",
    "バッグ", "リュック", "財布", "キーケース", "ポーチ", "帽子", "キャップ", "靴", "スニーカー", "サンダル",
    "腕時計", "時計", "ネックレス", "ピアス", "イヤリング", "指輪", "リング", "ブレスレット",
    "化粧水", "美容液", "乳液", "クリーム", "シャンプー", "トリートメント", "洗顔", "日焼け止め", "サプリ",
    "サプリメント", "プロテイン", "枕", "マットレス", "布団", "ソファ", "ラグ", "カーテン", "照明", "ライト",
    "水筒", "タンブラー", "マグカップ", "食器", "フライパン", "鍋", "ケース", "カバー", "充電器", "ケーブル",
    "イヤホン", "ヘッドホン", "スピーカー", "スマホケース", "フィルム", "収納", "ラック", "棚",
}

_SPLIT = re.compile(r"[\s　/|｜,、。!！?？()（）\[\]［］「」『』【】<>＜＞《》〈〉★☆◆◇■□●○◎♪※+＋&＆~〜～:：;；=＝\"'”“’‘#＃*＊]+")
_NUMERIC = re.compile(r"^[\d.,:/x×\-]+(cm|mm|m|g|kg|ml|l|個|枚|本|点|cc|インチ|歳|才)?$")


def normalize_keyword(kw: str) -> str:
    """表記ゆれを吸収した比較用キーワード（全角半角・大文字小文字・語順・空白）"""
    s = unicodedata.normalize("NFKC", str(kw)).lower().strip()
    parts = sorted(p for p in re.split(r"\s+", s) if p)
    return " ".join(parts)


def display_keyword(tokens: Iterable[str]) -> str:
    return " ".join(tokens)


def tokenize(text: str) -> List[str]:
    """商品名などを検索語の単位に分ける（重複は先頭のみ残し、出現順を保つ）"""
    s = unicodedata.normalize("NFKC", str(text))
    out: List[str] = []
    seen: Set[str] = set()
    for t in _SPLIT.split(s):
        t = t.strip("-_.")
        low = t.lower()
        if len(t) < 2 or low in STOP_WORDS or _NUMERIC.match(low):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(t)
    return out


def token_document_frequency(names: Iterable[str]) -> Counter:
    """店舗内の商品名で、各語が何商品に出てくるか"""
    df: Counter = Counter()
    for n in names:
        df.update({t.lower() for t in tokenize(n)})
    return df


def is_anchor(token: str, doc_freq: Counter, n_items: int, extra_anchors: Set[str] = frozenset()) -> bool:
    """ブランド・カテゴリーのように、他の語と組み合わせる「軸」になる語か"""
    low = token.lower()
    if low in CATEGORY_WORDS or low in extra_anchors:
        return True
    # 複数商品の商品名に共通して出る語は、ブランド名やカテゴリー名であることが多い
    return n_items >= 3 and doc_freq.get(low, 0) >= max(2, int(n_items * 0.3))


def title_candidates(
    name: str,
    doc_freq: Counter,
    n_items: int,
    max_tokens: int = 10,
    limit: int = 15,
    extra_anchors: Set[str] = frozenset(),
) -> List[Tuple[str, float, str]]:
    """商品名から2語の組み合わせ候補を作る。

    extra_anchors: ブランド名として扱う語（1語で登録済みのキーワードなど。例: blokees, ブロッキーズ）
    戻り値: [(キーワード, スコア, 根拠), ...] スコアの高い順
    """
    tokens = tokenize(name)[:max_tokens]
    scored: Dict[str, Tuple[str, float, str]] = {}
    for (i, a), (j, b) in combinations(enumerate(tokens), 2):
        a_anchor = is_anchor(a, doc_freq, n_items, extra_anchors)
        b_anchor = is_anchor(b, doc_freq, n_items, extra_anchors)
        if a_anchor and b_anchor and not (a.lower() in CATEGORY_WORDS) ^ (b.lower() in CATEGORY_WORDS):
            # 「ブランド＋ブランド」「カテゴリー＋カテゴリー」は意図があいまいなので除外
            continue
        score = 0.0
        if a_anchor != b_anchor:
            score += 2.0  # 固有の語 × 軸の語（例: スパイダーマン フィギュア）
            reason = "固有語×ブランド/カテゴリー"
        elif a_anchor and b_anchor:
            score += 1.5  # ブランド × カテゴリー（例: blokees フィギュア）
            reason = "ブランド×カテゴリー"
        else:
            score += 1.0  # 固有の語 × 固有の語（例: スパイダーマン マーベル）
            reason = "固有語の組み合わせ"
        if j == i + 1:
            score += 0.5  # 商品名で隣り合う語は、実際の検索語の並びに近い
        score *= 1.0 / (1.0 + 0.08 * i)  # 商品名の前方ほど重要な語が置かれている
        if re.fullmatch(r"[a-z]+", a.lower()) and re.fullmatch(r"[a-z]+", b.lower()):
            continue  # 英字だけの語同士（例: Blokees MARVEL）はブランドの重複になりやすい
        # 並びは「ブランド → 固有の語 → カテゴリー語」（例: Blokees スパイダーマン / スパイダーマン フィギュア）
        def _rank(t: str) -> int:
            low = t.lower()
            if low in CATEGORY_WORDS:
                return 2
            return 0 if is_anchor(t, doc_freq, n_items, extra_anchors) else 1
        kw = display_keyword(sorted([a, b], key=_rank))
        key = normalize_keyword(kw)
        if key not in scored or scored[key][1] < score:
            scored[key] = (kw, score, f"商品名から生成（{reason}）")
    return sorted(scored.values(), key=lambda x: -x[1])[:limit]


def relevance(keyword: str, item_name: str) -> float:
    """キーワードの語のうち、商品名に含まれる語の割合（0〜1）"""
    kw_tokens = [t for t in re.split(r"\s+", unicodedata.normalize("NFKC", keyword).lower()) if t]
    if not kw_tokens:
        return 0.0
    name = unicodedata.normalize("NFKC", item_name).lower()
    return sum(1 for t in kw_tokens if t in name) / len(kw_tokens)
