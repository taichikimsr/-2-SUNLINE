"""Discord Webhook 通知"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from .analyzer import CONDITION_NAMES, Deal

log = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

SOURCE_LABEL = {
    "reference": "設定した基準価格",
    "sold_median": "売り切れ商品の中央値",
}


def yen(n: int | None) -> str:
    return "—" if n is None else f"¥{n:,}"


def deal_embed(watch_name: str, d: Deal) -> dict:
    it, m = d.item, d.market
    color = 0xE67E22 if d.suspicious else 0x2ECC71
    lines = [
        f"**価格** {yen(it.price)}　**相場** {yen(m.price)}（{SOURCE_LABEL.get(m.source, m.source)}）",
        f"**相場より {d.discount * 100:.0f}% 安い**（差額 {yen(m.price - it.price)}）",
    ]
    if d.price_drop_from:
        lines.append(f"値下げ: {yen(d.price_drop_from)} → {yen(it.price)}")
    lines.append(
        f"売り切れ中央値 {yen(m.sold_median)}（{m.sold_samples}件）／"
        f"出品中中央値 {yen(m.on_sale_median)}（{m.on_sale_samples}件）"
    )
    extra = []
    if it.condition_id in CONDITION_NAMES:
        extra.append(f"状態: {CONDITION_NAMES[it.condition_id]}")
    if it.is_shop:
        extra.append("メルカリShops")
    if it.is_auction:
        extra.append("オークション（表示価格は現在価格）")
    if extra:
        lines.append(" ／ ".join(extra))
    if d.suspicious:
        lines.append("⚠️ 相場に比べ安すぎます。偽物・付属品のみ・誤記などの可能性があるため、写真と説明文を必ず確認してください。")

    embed = {
        "title": it.name[:250] or it.id,
        "url": it.url,
        "description": "\n".join(lines)[:4000],
        "color": color,
        "footer": {"text": f"監視: {watch_name}"[:2000]},
    }
    if it.thumbnail:
        embed["thumbnail"] = {"url": it.thumbnail}
    return embed


class DiscordNotifier:
    def __init__(self, webhook_url: str, username: str = "メルカリお得品アラート",
                 dry_run: bool = False):
        self.webhook_url = webhook_url
        self.username = username
        self.dry_run = dry_run

    def _send(self, payload: dict) -> None:
        payload = {"username": self.username, **payload}
        if self.dry_run:
            log.info("[dry-run] 通知内容: %s", payload)
            return
        for attempt in range(5):
            r = requests.post(self.webhook_url, json=payload, timeout=20)
            if r.status_code == 429:  # Discord のレート制限
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Discord 送信失敗 HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(1)  # 連投しすぎないよう間隔を空ける
            return
        raise RuntimeError("Discord のレート制限により送信できませんでした")

    def send_deals(self, watch_name: str, deals: list[Deal]) -> None:
        # 1 メッセージに埋め込みは最大 10 件
        for i in range(0, len(deals), 10):
            chunk = deals[i:i + 10]
            content = f"🔔 **{watch_name}** でお得な出品 {len(deals)} 件" if i == 0 else ""
            self._send({"content": content, "embeds": [deal_embed(watch_name, d) for d in chunk]})

    def send_text(self, text: str) -> None:
        # content の上限は 2000 文字
        for i in range(0, len(text), 1900):
            self._send({"content": text[i:i + 1900]})


def now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")
