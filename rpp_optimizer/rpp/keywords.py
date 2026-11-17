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
    "特別価格", "新店open", "お試し", "本体", "詰め替え", "詰替", "単品", "選べる", "選択可能", "塗装済み", "完成品",
    "組み立て式", "組み立てキット", "組立", "リアル", "映画", "盲盒", "diy", "公式ライセンス", "ライセンス", "変装版",
    "特別版", "高評価", "定番", "最新", "通販", "安心", "使い捨て", "追跡可能", "ベストセラー", "デパコス", "母の日",
    "国内正規品", "正規", "新店", "open", "初心者向け", "男女兼用", "自宅用", "室内遊び", "ドラッグストア", "おすすめ",
    "人気商品", "売れ筋", "まとめ", "選択", "付き", "付", "入り", "対策", "パーツ", "1box", "box",
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
    "ローション", "化粧下地", "クレンジング", "クレンジングオイル", "洗顔フォーム", "パック", "マスク", "リップクリーム",
    "リップ", "リップオイル", "ヘアオイル", "コンディショナー", "ボディソープ", "ハンドクリーム", "ファンデーション",
    "オールインワン", "知育玩具", "ホビー", "ふきん", "キッチンクロス", "バスマット", "タオル", "ティッシュケース",
    "ヘルメット", "作業灯", "懐中電灯", "キーホルダー", "自撮り棒", "充電ケーブル", "チューブ", "バンド",
}

# 悩み・特徴を表す語。カテゴリー語とだけ組み合わせる（例: 乾燥肌 化粧水）。ブランドとの組み合わせは意図があいまいなので作らない
ATTRIBUTE_WORDS: Set[str] = {
    "保湿", "高保湿", "乾燥", "乾燥肌", "敏感肌", "脂性肌", "混合肌", "毛穴", "毛穴ケア", "エイジングケア", "美白", "透明感",
    "ハリ", "弾力", "うるおい", "しっとり", "さっぱり", "とてもしっとり", "しっとりタイプ", "さっぱりタイプ", "低刺激",
    "無香料", "無着色", "無添加", "医薬部外品", "薬用", "大容量", "防水", "軽量", "超軽量", "折りたたみ", "充電式", "静音",
    "抗菌", "速乾", "吸水", "厚手", "薄型", "高級", "高級感", "シンプル", "北欧", "子供用", "女性用", "男性用", "日中用",
    "夜用", "uvカット", "くすみケア", "ダメージケア", "サラサラ", "補修", "保温", "保冷", "強力", "明るい", "コンパクト",
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
        if _DIGITS3.search(low) or re.search(r"\d+(歳|才)", low) or re.fullmatch(r"[a-z]{1,2}\d*", low) \
                or re.fullmatch(r"(vol\.?\d+|\d+box|\d+個入り?|\d+本入り?|\d+枚入り?|\d+(ml|g|cm|mm|w|mah))", low):
            continue  # 型番・年齢表記（例: 81146GV06, 9歳以上）は検索語になりにくい
        if re.search(r"(個入|本入|枚入|box\d|\dbox)", low):
            continue  # 入数表記（例: 1BOX9個入）
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


def _is_category(token: str, cats: Set[str]) -> bool:
    low = token.lower()
    return low in cats or any(len(c) >= 2 and low.endswith(c) and len(low) - len(c) <= 4 for c in cats)


def _is_attribute(token: str) -> bool:
    return token.lower() in ATTRIBUTE_WORDS


_DIGITS3 = re.compile(r"\d.*\d.*\d")


def is_anchor(token: str, doc_freq: Counter, n_items: int, extra_anchors: Set[str] = frozenset()) -> bool:
    """ブランド・カテゴリーのように、他の語と組み合わせる「軸」になる語か"""
    low = token.lower()
    if low in CATEGORY_WORDS or low in extra_anchors:
        return True
    # 複数商品の商品名に共通して出る語は、ブランド名やカテゴリー名であることが多い
    return n_items >= 3 and doc_freq.get(low, 0) >= max(2, int(n_items * 0.3))


GENERIC_CATEGORY_NAMES = {"その他", "インテリア・雑貨", "ファッション", "セレクション", "新着", "おすすめ", "セール",
                          "ランキング", "均一", "ポッキリ", "全商品", "トップ"}


def category_terms(path: str) -> Tuple[Set[str], Set[str]]:
    """店舗カテゴリ（例: コスメ・ビューティー\\リップケア\\リップクリーム ｜ BLOKEES ブロッキーズ）から
    カテゴリー語（最下層）とブランド語（ブランド名らしい階層）を取り出す"""
    cats: Set[str] = set()
    brands: Set[str] = set()
    for one in str(path).split("|"):
        parts = [p.strip() for p in re.split(r"[\\>/]", unicodedata.normalize("NFKC", one)) if p.strip()]
        if not parts:
            continue
        if any(g in parts[0] for g in GENERIC_CATEGORY_NAMES) or "円" in parts[0]:
            continue
        if len(parts) >= 2:
            cats.add(parts[-1].lower())
        else:
            # 1階層だけのカテゴリはブランド名のことが多い（例: BLOKEES ブロッキーズ / 資生堂・ELIXIR）
            for w in re.split(r"[\s・]+", parts[0]):
                if len(w) >= 2:
                    brands.add(w.lower())
    return cats, brands


def title_candidates(
    name: str,
    doc_freq: Counter,
    n_items: int,
    max_tokens: int = 10,
    limit: int = 15,
    extra_anchors: Set[str] = frozenset(),
    extra_categories: Set[str] = frozenset(),
    aliases: Set[frozenset] = frozenset(),
) -> List[Tuple[str, float, str]]:
    """商品名から2語の組み合わせ候補を作る。

    extra_anchors: ブランド名として扱う語（1語で登録済みのキーワードなど。例: blokees, ブロッキーズ）
    戻り値: [(キーワード, スコア, 根拠), ...] スコアの高い順
    """
    tokens = tokenize(name)[:max_tokens]
    cats = CATEGORY_WORDS | set(extra_categories)
    # 店舗カテゴリの最下層（例: リップクリーム）は、商品名に同じ語があるときだけカテゴリー語として扱う
    # （店舗カテゴリ名は店舗独自の表記や誤字があり得るため、検索語として新たに足すことはしない）
    scored: Dict[str, Tuple[str, float, str]] = {}
    has_brand = any(is_anchor(t, doc_freq, n_items, extra_anchors) and not _is_category(t, cats) for t in tokens)
    if not has_brand:
        # ブランドの無い一般的な商品は、商品の種類そのもの（商品名の先頭の語）で検索されることが多い
        for i, t in enumerate(tokens[:3]):
            if len(t) >= 4 and not _is_attribute(t):
                key = normalize_keyword(t)
                scored[key] = (t, 2.2 - 0.3 * i, "商品名から生成（商品の種類を表す語）")
    for (i, a), (j, b) in combinations(enumerate(tokens), 2):
        a_cat, b_cat = _is_category(a, cats), _is_category(b, cats)
        a_attr, b_attr = _is_attribute(a), _is_attribute(b)
        if frozenset((a.lower(), b.lower())) in aliases:
            continue  # 同じブランドの英字表記とカタカナ表記（例: Attenir アテニア）
        if a.lower() in b.lower() or b.lower() in a.lower():
            continue  # 片方がもう片方を含む（例: ジェミールフラントリートメント トリートメント）
        if a_attr or b_attr:
            # 悩み・特徴の語は、カテゴリー語との組み合わせだけ作る（例: 乾燥肌 化粧水）
            if (a_attr and b_cat and not a_cat) or (b_attr and a_cat and not b_cat):
                score = 1.6 * (1.0 / (1.0 + 0.05 * i))
                kw = display_keyword([a, b] if a_attr else [b, a])
                key = normalize_keyword(kw)
                if key not in scored or scored[key][1] < score:
                    scored[key] = (kw, score, "商品名から生成（悩み・特徴×カテゴリー）")
            continue
        if a_cat and b_cat:
            continue  # カテゴリー語同士（例: ローション 乳液）
        a_anchor = is_anchor(a, doc_freq, n_items, extra_anchors) or a_cat
        b_anchor = is_anchor(b, doc_freq, n_items, extra_anchors) or b_cat
        if a_anchor and b_anchor and not a_cat ^ b_cat:
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
            if _is_category(t, cats):
                return 2
            if _is_attribute(t):
                return 1
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


_ASCII = re.compile(r"^[a-z][a-z0-9.\-]*$")
_KATAKANA = re.compile(r"^[\u30a0-\u30ffー・]+$")


def script_aliases(names: Iterable[str], min_df: int = 2) -> Set[frozenset]:
    """英字の語とカタカナの語が、ほぼ必ず同じ商品名に一緒に出てくる組み合わせ（＝同じブランドの別表記）を探す"""
    toks = [{t.lower() for t in tokenize(n)} for n in names]
    df: Counter = Counter()
    for ts in toks:
        df.update(ts)
    asc = [t for t, c in df.items() if c >= min_df and _ASCII.match(t)]
    kat = [t for t, c in df.items() if c >= min_df and _KATAKANA.match(t)]
    out: Set[frozenset] = set()
    for a in asc:
        for k in kat:
            both = sum(1 for ts in toks if a in ts and k in ts)
            if both >= min_df and both >= 0.8 * min(df[a], df[k]) and both >= 0.5 * max(df[a], df[k]):
                out.add(frozenset((a, k)))
    return out
