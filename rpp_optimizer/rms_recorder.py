"""RMSでの操作を記録する（パスワード・入力文字は記録しない）

普通のChrome（ツール専用プロフィール）に接続し、人が行った操作のうち
「どのボタン・リンクを押したか」「どの画面に移動したか」「どのファイルを選んだか」「どの選択肢を選んだか」
だけを記録する。文字入力欄・パスワード欄に入力した内容は、一切記録しない。

記録は rms_recorded_steps.json に保存され、自動操作（rms_steps.py）を作るための材料にする。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

# ページに埋め込むスクリプト：クリック・選択・ファイル選択を検知して、要素の特徴だけを送る
RECORDER_JS = r"""
(() => {
  if (window.__rmsRecInstalled) return;
  window.__rmsRecInstalled = true;
  const clean = (s, n = 80) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let p = el.tagName.toLowerCase();
      if (el.id) { p += "#" + el.id; parts.unshift(p); break; }
      const sib = el.parentElement ? Array.from(el.parentElement.children).filter(c => c.tagName === el.tagName) : [];
      if (sib.length > 1) p += `:nth-of-type(${sib.indexOf(el) + 1})`;
      parts.unshift(p);
      el = el.parentElement;
    }
    return parts.join(" > ");
  };
  const describe = (el) => {
    const label = el.labels && el.labels.length ? clean(el.labels[0].innerText) : "";
    const type = (el.getAttribute("type") || "").toLowerCase();
    const isSecret = type === "password";
    return {
      tag: el.tagName.toLowerCase(), type, id: el.id || "", name: el.getAttribute("name") || "",
      role: el.getAttribute("role") || "", aria: clean(el.getAttribute("aria-label")),
      title: clean(el.getAttribute("title")), label,
      text: isSecret ? "" : clean(el.innerText || ""),
      value: (el.tagName === "INPUT" && ["submit", "button", "reset"].includes(type)) ? clean(el.value) : "",
      href: el.getAttribute("href") || "", css: cssPath(el),
    };
  };
  const send = (kind, el, extra) => {
    try { window.__rmsRec(JSON.stringify(Object.assign({ kind, url: location.href, el: describe(el) }, extra || {}))); }
    catch (e) {}
  };
  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("a,button,input,select,label,[role=button],[role=link],[role=tab],[onclick],td,li,span,div") || ev.target;
    const type = (el.getAttribute && (el.getAttribute("type") || "").toLowerCase()) || "";
    if (el.tagName === "INPUT" && !["submit", "button", "reset", "checkbox", "radio", "file", "image"].includes(type)) {
      return; // 文字入力欄のクリックは記録しない
    }
    send("click", el);
  }, true);
  document.addEventListener("change", (ev) => {
    const el = ev.target;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (el.tagName === "SELECT") {
      const o = el.options[el.selectedIndex];
      send("select", el, { option_text: o ? clean(o.text) : "", option_value: o ? o.value : "" });
    } else if (type === "file") {
      send("file", el, { files: Array.from(el.files || []).map(f => f.name) });
    } else if (type === "checkbox" || type === "radio") {
      send("check", el, { checked: el.checked, option_value: el.value });
    } else if (type === "date" || type === "month") {
      send("date", el, { date_value: el.value });  // 日付は記録する（レポートの期間指定に必要）
    } else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      send("typed", el, { note: "入力内容は記録していません" });
    }
  }, true);
})();
"""


def record(connect, out_path: str, start_url: str) -> int:
    """connect: rms_bot.connect（専用Chromeに接続して (context, page) を返す関数）"""
    from playwright.sync_api import sync_playwright

    steps = []
    lock = threading.Lock()

    def add(entry):
        entry["time"] = datetime.now().strftime("%H:%M:%S")
        with lock:
            steps.append(entry)
        k = entry.get("kind")
        el = entry.get("el", {})
        what = el.get("text") or el.get("value") or el.get("aria") or el.get("title") or el.get("name") or el.get("id") or el.get("tag", "")
        print(f"  記録 {len(steps):>3}: {k:<8} {what[:40]}  ({entry.get('url', '')[:70]})")

    with sync_playwright() as p:
        ctx, page = connect(p, start_url)
        ctx.expose_binding("__rmsRec", lambda source, payload: add(json.loads(payload)))
        ctx.add_init_script(RECORDER_JS)

        def watch(pg):
            pg.on("framenavigated", lambda fr: fr == pg.main_frame and add({"kind": "navigate", "url": fr.url}))
            pg.on("download", lambda d: add({"kind": "download", "url": pg.url, "file": d.suggested_filename}))
            pg.on("filechooser", lambda fc: add({"kind": "filechooser", "url": pg.url}))
            for fr in pg.frames:
                try:
                    fr.evaluate(RECORDER_JS)
                except Exception:  # noqa: BLE001
                    pass

        for pg in ctx.pages:
            watch(pg)
        ctx.on("page", lambda pg: (add({"kind": "new_tab", "url": pg.url}), watch(pg)))

        print("記録中です。開いているChromeで操作してください（パスワード・入力した文字は記録しません）。")
        print("終わったら、この画面で Enter を押してください。")
        stop = threading.Event()
        threading.Thread(target=lambda: (input(), stop.set()), daemon=True).start()
        while not stop.is_set():
            page.wait_for_timeout(300)  # イベントを処理しながら待つ

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"recorded_at": datetime.now().isoformat(timespec="seconds"), "steps": steps}, f,
                  ensure_ascii=False, indent=2)
    print(f"{len(steps)}件の操作を {out_path} に保存しました。このファイルを送ってください。")
    return 0
