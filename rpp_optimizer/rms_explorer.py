"""RMSの画面構成を自動で調べる（閲覧のみ）

ログイン済みの普通のChromeに接続し、RPP（広告）に関係しそうなリンク・タブだけをたどって、
各画面の「リンク」「ボタン」「入力欄（名前と種類のみ）」「選択肢」「ファイル選択欄」を記録する。

安全のための制限
  - 押すのは、RPP関係の語を含むリンク・タブ・メニューだけ
  - 送信・登録・保存・削除・アップロード・変更・停止・ログアウトなどの語を含むものは押さない
  - フォームの送信はしない。入力欄には何も入力しない
  - 入力欄の中身（値）は記録しない
  - 1画面ごとに間隔を空け、調べる画面数に上限を設ける
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

FOLLOW_WORDS = ("rpp", "広告", "プロモーション", "パフォーマンス", "レポート", "キャンペーン", "キーワード",
                "一括", "csv", "除外", "予算", "商品・キーワード", "商品別", "キーワード別", "設定", "アップロード",
                "promotion", "report", "upload")
# 押さない語（リンク・タブのみ押す。アップロード画面などを「開く」だけのリンクは押してよい）
NEVER_WORDS = ("送信", "登録する", "保存", "削除", "停止", "適用", "確定", "実行", "購入", "申込",
               "申し込", "ログアウト", "logout", "退会", "解約", "支払", "契約", "同意", "delete", "submit")
ALLOWED_HOST = re.compile(r"(^|\.)rms\.rakuten\.co\.jp$|(^|\.)rakuten\.co\.jp$")

# 画面の構造を集めるスクリプト（入力欄の値は集めない）
SNAPSHOT_JS = r"""
() => {
  const clean = (s, n = 80) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const vis = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const label = (el) => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    return clean(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("title") || "");
  };
  const css = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let p = el.tagName.toLowerCase();
      if (el.id) { p += "#" + el.id; parts.unshift(p); break; }
      const sib = el.parentElement ? Array.from(el.parentElement.children).filter(c => c.tagName === el.tagName) : [];
      if (sib.length > 1) p += `:nth-of-type(${sib.indexOf(el) + 1})`;
      parts.unshift(p); el = el.parentElement;
    }
    return parts.join(" > ");
  };
  const links = Array.from(document.querySelectorAll("a,[role=link],[role=tab],[role=menuitem]")).filter(vis).slice(0, 300)
    .map(a => ({ text: clean(a.innerText || a.getAttribute("aria-label") || a.title), href: a.getAttribute("href") || "",
                 role: a.getAttribute("role") || a.tagName.toLowerCase(), css: css(a) }));
  const buttons = Array.from(document.querySelectorAll("button,input[type=submit],input[type=button],input[type=image],[role=button]"))
    .filter(vis).slice(0, 200)
    .map(b => ({ text: clean(b.innerText || b.value || b.getAttribute("aria-label") || b.title), id: b.id || "",
                 name: b.getAttribute("name") || "", type: b.getAttribute("type") || "", css: css(b) }));
  const inputs = Array.from(document.querySelectorAll("input,textarea")).filter(i => !["submit","button","image","hidden"].includes((i.type||"").toLowerCase()))
    .slice(0, 200).map(i => ({ type: (i.type || "").toLowerCase(), name: i.getAttribute("name") || "", id: i.id || "",
                               label: label(i), accept: i.getAttribute("accept") || "", css: css(i) }));
  const selects = Array.from(document.querySelectorAll("select")).slice(0, 100).map(s => ({
    name: s.getAttribute("name") || "", id: s.id || "", label: label(s), css: css(s),
    options: Array.from(s.options).slice(0, 60).map(o => ({ text: clean(o.text, 40), value: o.value })) }));
  const forms = Array.from(document.querySelectorAll("form")).slice(0, 50).map(f => ({
    action: f.getAttribute("action") || "", method: (f.getAttribute("method") || "get").toLowerCase(), id: f.id || "",
    name: f.getAttribute("name") || "", enctype: f.getAttribute("enctype") || "" }));
  const headings = Array.from(document.querySelectorAll("h1,h2,h3,legend,th")).filter(vis).slice(0, 60).map(h => clean(h.innerText, 60));
  return { title: document.title, headings, links, buttons, inputs, selects, forms };
}
"""


def _wants(text: str, href: str) -> bool:
    t = (text or "").lower()
    h = (href or "").lower()
    if any(w in t for w in NEVER_WORDS) or any(w in h for w in ("logout", "delete", "remove")):
        return False
    return any(w in t for w in FOLLOW_WORDS) or "rpp" in h


def explore(connect, out_path: str, start_url: str, max_pages: int = 30, wait_ms: int = 2500,
            allowed_host=ALLOWED_HOST) -> int:
    from playwright.sync_api import sync_playwright

    pages = []
    seen_urls = set()
    seen_clicks = set()
    with sync_playwright() as p:
        ctx, page = connect(p, start_url)
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)
        if "glogin" in page.url:
            print("ログインが切れています。py rms_bot.py login でログインしてから、もう一度実行してください。")
            return 2
        queue = [("goto", page.url, None, 0)]
        while queue and len(pages) < max_pages:
            kind, target, via, depth = queue.pop(0)
            try:
                if kind == "goto":
                    if target in seen_urls:
                        continue
                    page.goto(target, wait_until="domcontentloaded", timeout=60_000)
                else:  # SPA のタブ・メニュー（href の無いもの）: 元の画面に戻ってから押す
                    page.goto(via, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(wait_ms)
                    page.locator(target).first.click(timeout=10_000)
                page.wait_for_timeout(wait_ms)
            except Exception as e:  # noqa: BLE001
                print(f"  開けませんでした: {target[:80]}（{str(e)[:60]}）")
                continue
            if "glogin" in page.url:
                print("途中でログインが切れたため、調査を終了します。")
                break
            url = page.url
            frames = []
            for fr in page.frames:
                try:
                    snap = fr.evaluate(SNAPSHOT_JS)
                except Exception:  # noqa: BLE001
                    continue
                frames.append({"frame_url": fr.url, **snap})
            if url in seen_urls and kind == "goto":
                continue
            seen_urls.add(url)
            pages.append({"url": url, "reached_by": {"kind": kind, "target": target, "from": via}, "depth": depth,
                          "frames": frames})
            title = frames[0]["title"] if frames else ""
            print(f"  調査 {len(pages):>2}: {title[:40]}  {url[:80]}")
            if depth >= 3:
                continue
            for fr in frames:
                for ln in fr["links"]:
                    if not _wants(ln["text"], ln["href"]):
                        continue
                    href = ln["href"]
                    if href and not href.startswith(("javascript:", "#")):
                        absu = urljoin(fr["frame_url"] or url, href)
                        host = urlparse(absu).hostname or ""
                        if allowed_host.search(host) and absu not in seen_urls:
                            queue.append(("goto", absu, url, depth + 1))
                    elif ln["css"] and (url, ln["css"]) not in seen_clicks and fr["frame_url"] == url:
                        seen_clicks.add((url, ln["css"]))
                        queue.append(("click", ln["css"], url, depth + 1))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"explored_at": datetime.now().isoformat(timespec="seconds"), "start": start_url, "pages": pages},
                  f, ensure_ascii=False, indent=1)
    print(f"{len(pages)}画面を調べ、{out_path} に保存しました。このファイルを送ってください。")
    return 0
