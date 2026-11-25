"""メルカリお得品アラート 実行エントリポイント

使い方:
    python -m mercari_alert.main                 # 通常実行（Discordへ通知）
    python -m mercari_alert.main --dry-run       # 通知せずログに表示（記録も更新しない）
    python -m mercari_alert.main --test-notify   # Discord にテスト通知を送るだけ
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import yaml

from .analyzer import DEFAULT_EXCLUDE_WORDS, compute_market, filter_items, find_deals
from .client import STATUS_ON_SALE, STATUS_SOLD_OUT, MercariClient, MercariError
from .notifier import SOURCE_LABEL, DiscordNotifier, now_jst_str, yen
from .state import SeenStore

log = logging.getLogger("mercari_alert")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("watches"):
        raise SystemExit(f"{path} に watches（監視する商品）が1件もありません")
    for i, w in enumerate(cfg["watches"]):
        if not w.get("keyword"):
            raise SystemExit(f"watches の {i + 1} 件目に keyword がありません")
        w.setdefault("name", w["keyword"])
    return cfg


def _words(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return v.split()
    return [str(x) for x in v]


def run_watch(client: MercariClient, w: dict, s: dict, store: SeenStore) -> tuple[list, str]:
    """1 件の監視条件を処理し、(通知するお得品, 状況メモ) を返す"""
    exclude_words = (
        (DEFAULT_EXCLUDE_WORDS if s.get("use_default_exclude_words", True) else [])
        + _words(s.get("exclude_words"))
        + _words(w.get("exclude_words"))
    )
    params = dict(
        keyword=w["keyword"],
        exclude_keyword=" ".join(_words(w.get("search_exclude"))),
        price_min=w.get("price_min"),
        price_max=w.get("price_max"),
        condition_ids=w.get("condition_ids"),
        server_side_filters=s.get("server_side_filters", True),
    )
    local = dict(
        exclude_words=exclude_words,
        price_min=w.get("price_min"),
        price_max=w.get("price_max"),
        condition_ids=w.get("condition_ids"),
        include_shops=w.get("include_shops", s.get("include_shops", True)),
        include_auctions=w.get("include_auctions", s.get("include_auctions", True)),
        required_words=_words(w.get("required_words")),
    )

    on_sale = filter_items(client.search(status=STATUS_ON_SALE, **params), **local)
    sold = []
    if not w.get("reference_price") or s.get("always_fetch_sold", True):
        sold = filter_items(client.search(status=STATUS_SOLD_OUT, **params), **local)

    market = compute_market(
        sold, on_sale,
        reference_price=w.get("reference_price"),
        min_sold_samples=int(s.get("min_sold_samples", 5)),
    )
    if market.price is None:
        return [], (f"相場を算出できず（売り切れ {market.sold_samples} 件 < 必要 "
                    f"{s.get('min_sold_samples', 5)} 件）。reference_price の設定を推奨")

    deals = find_deals(
        on_sale, market,
        discount_threshold=float(w.get("discount_threshold", s.get("discount_threshold", 0.25))),
        suspicious_ratio=float(w.get("suspicious_ratio", s.get("suspicious_ratio", 0.35))),
    )
    to_notify = []
    for d in deals:
        ok, prev = store.should_alert(d.item.id, d.item.price)
        if ok:
            d.price_drop_from = prev
            to_notify.append(d)
    to_notify = to_notify[: int(s.get("max_alerts_per_watch", 10))]
    note = (f"相場 {yen(market.price)}（{SOURCE_LABEL.get(market.source)}）、"
            f"出品中 {len(on_sale)} 件、条件該当 {len(deals)} 件、新規通知 {len(to_notify)} 件")
    return to_notify, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="メルカリお得品アラート")
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.yaml"))
    ap.add_argument("--state", default=os.path.join(BASE_DIR, "state", "seen.json"))
    ap.add_argument("--dry-run", action="store_true", help="通知せず結果をログに出す")
    ap.add_argument("--test-notify", action="store_true", help="Discordにテスト通知を送る")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        log.error("環境変数 DISCORD_WEBHOOK_URL が未設定です（--dry-run なら不要）")
        return 2
    notifier = DiscordNotifier(webhook, dry_run=args.dry_run)

    if args.test_notify:
        notifier.send_text(f"✅ メルカリお得品アラートのテスト通知です（{now_jst_str()}）")
        log.info("テスト通知を送信しました")
        return 0

    cfg = load_config(args.config)
    s = cfg.get("settings") or {}
    interval = s.get("request_interval_sec", [4, 8])
    client = MercariClient(
        interval_sec=(float(interval[0]), float(interval[1])),
        user_agent=s.get("user_agent", "python-mercari"),
    )
    store = SeenStore(args.state)

    summary, errors, total = [], [], 0
    for w in cfg["watches"]:
        if w.get("enabled", True) is False:
            continue
        name = w["name"]
        try:
            deals, note = run_watch(client, w, s, store)
        except MercariError as e:
            errors.append(f"・{name}: {e}")
            log.error("[%s] 取得失敗: %s", name, e)
            continue
        except Exception as e:  # 想定外の応答形式など。他の監視は続行する
            errors.append(f"・{name}: 想定外のエラー {type(e).__name__}: {e}")
            log.exception("[%s] 想定外のエラー", name)
            continue
        log.info("[%s] %s", name, note)
        summary.append(f"・{name}: {note}")
        if deals:
            try:
                notifier.send_deals(name, deals)
            except Exception as e:
                errors.append(f"・{name}: Discord送信失敗 {e}")
                log.error("[%s] Discord送信失敗: %s", name, e)
                continue
            if not args.dry_run:
                for d in deals:
                    store.mark(d.item.id, d.item.price, name)
            total += len(deals)

    if not args.dry_run:
        store.prune()
        store.save()

    try:
        if errors:
            hint = ""
            if any("HTTP 403" in e or "HTTP 401" in e for e in errors):
                hint = ("\n※403/401 はメルカリ側でアクセスが拒否されています。"
                        "実行環境のIPがブロックされたか、API仕様が変わった可能性があります。")
            notifier.send_text(f"⚠️ チェック中にエラーがありました（{now_jst_str()}）\n"
                               + "\n".join(errors) + hint)
        if s.get("notify_summary", True):
            notifier.send_text(f"📋 チェック完了（{now_jst_str()}）新規通知 {total} 件\n"
                               + "\n".join(summary or ["（成功した監視なし）"]))
    except Exception as e:
        log.error("まとめ通知の送信に失敗: %s", e)

    # 全監視が失敗した場合は異常終了にして GitHub Actions 上でも気付けるようにする
    return 1 if errors and not summary else 0


if __name__ == "__main__":
    sys.exit(main())
