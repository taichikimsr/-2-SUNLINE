"""RMS自動運用ボット（開発中）

ブラウザを自動で操作して、毎週の運用を行う。
  ① RMSからRPPレポートをダウンロード
  ② 運用エンジンでアクションプランを作成
  ③ RMSへ一括アップロード（--apply を付けたときだけ）

安全のための方針
  - パスワードは保存・自動入力しない。最初に人が手動でログインし（Viberの認証コードを含む）、
    そのブラウザの状態（このPCの rpp_optimizer/.rms_profile フォルダ）を使い回す。
    ログインが切れていたら、自動で再ログインはせずに止まって知らせる。
    （自動ログインの失敗を繰り返すと、利用規制がかかったという報告があるため）
  - --apply を付けない限り、RMSの設定は変えない（CSVの作成まで）
  - 1回の変更件数が上限を超えたら反映しない

RMSの画面操作（どのボタンを押すか）は、まだ記録できていない。
「record」で実際の操作を1回記録し、記録したファイルを開発側に渡すと、自動操作に組み込める。

使い方（Windows、rpp_optimizer フォルダで）:
    py -m pip install playwright
    （ブラウザはPCに入っている Google Chrome または Microsoft Edge を使うため、
      「py -m playwright install chromium」は不要）
    py rms_bot.py login     … ブラウザが開くので手動でログイン（「信頼できる端末」に登録すると14日間コード不要）
    py rms_bot.py check     … ログイン状態の確認
    py rms_bot.py record    … RMSでの操作を記録（レポートのダウンロード・一括アップロード・月予算の変更）
    py rms_bot.py run       … 運用を1回実行（CSVの作成まで）
    py rms_bot.py run --apply … RMSへの反映まで実行（記録済みの操作が組み込まれてから）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PROFILE_DIR = os.path.join(BASE, ".rms_profile")       # ログイン状態を保存するブラウザのフォルダ（このPCのみ）
INBOX_DIR = os.path.join(BASE, "runs", "inbox")         # ダウンロードしたレポート
OUT_DIR = os.path.join(BASE, "runs", "output")          # 作成したCSV・運用記録
LOG_PATH = os.path.join(BASE, "runs", "bot.log")
RECORD_PATH = os.path.join(BASE, "rms_recorded_steps.py")

# RMSのURL（画面が変わった場合はここを変更）
RMS_LOGIN_URL = "https://glogin.rms.rakuten.co.jp/"
RMS_MAIN_URL = "https://mainmenu.rms.rakuten.co.jp/"

MAX_ACTIONS_PER_RUN = 300  # これを超える変更は自動では反映しない


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# PCに入っているブラウザを優先して使う（Playwright専用ブラウザのダウンロードを不要にするため）
CHANNELS = ("chrome", "msedge", None)  # None = Playwright専用のChromium（py -m playwright install chromium が必要）


def _browser(p, headless: bool):
    last = None
    for ch in CHANNELS:
        try:
            kw = {"channel": ch} if ch else {}
            return p.chromium.launch_persistent_context(PROFILE_DIR, headless=headless, accept_downloads=True,
                                                        locale="ja-JP", viewport={"width": 1400, "height": 900}, **kw)
        except Exception as e:  # noqa: BLE001  そのブラウザが無ければ次を試す
            last = e
    raise RuntimeError("Google Chrome も Microsoft Edge も起動できませんでした。どちらかをインストールしてください。"
                       f"（詳細: {last}）")


def _installed_channel() -> str:
    """codegen（操作の記録）に使うブラウザ"""
    import shutil

    candidates = {
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                   os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                   "/Applications/Google Chrome.app"],
        "msedge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                   r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                   "/Applications/Microsoft Edge.app"],
    }
    for ch, paths in candidates.items():
        if any(os.path.exists(x) for x in paths) or shutil.which(ch):
            return ch
    return ""


def is_logged_in(page) -> bool:
    page.goto(RMS_MAIN_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2000)
    # ログインしていなければ、ログイン画面（glogin.rms.rakuten.co.jp）へ移動する
    return "glogin" not in page.url


def cmd_login(_args) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(RMS_LOGIN_URL)
        print("ブラウザでRMSにログインしてください（Viberの認証コード入力、「信頼できる端末」への登録まで）。")
        input("RMSのメインメニューが表示されたら、この画面で Enter を押してください … ")
        ok = is_logged_in(page)
        ctx.close()
    log("ログイン状態を保存しました" if ok else "ログインを確認できませんでした。もう一度 login を実行してください")
    return 0 if ok else 1


def cmd_check(_args) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok = is_logged_in(page)
        ctx.close()
    log("ログイン中です" if ok else "ログインが切れています。py rms_bot.py login で再ログインしてください")
    return 0 if ok else 2


def cmd_record(_args) -> int:
    print("記録用のブラウザが開きます。次の操作を、普段どおり1回ずつ行ってください:")
    print("  1. RPPのパフォーマンスレポートで、キーワード別（直近30日）をCSVダウンロード")
    print("  2. 同じく商品別をCSVダウンロード")
    print("  3. 商品・キーワード設定 → 一括アップロード（キーワードCPC）でCSVを選んで送信 ※テスト用の1行のCSVで")
    print("  4. キャンペーン設定で月予算を変更する画面を開く（保存はしなくて構いません）")
    print(f"終わったらブラウザを閉じてください。操作は {RECORD_PATH} に保存されます。")
    print("※パスワードを入力した場合は記録に残るため、記録はログイン後の画面から始めてください。")
    cmd = [sys.executable, "-m", "playwright", "codegen", "--target", "python",
           "--user-data-dir", PROFILE_DIR, "-o", RECORD_PATH]
    ch = _installed_channel()
    if ch:
        cmd += ["--channel", ch]
    return subprocess.call(cmd + [RMS_MAIN_URL])


def cmd_run(args) -> int:
    from rpp import settings as st
    from rpp.pipeline import run_once

    conf = st.load()
    if args.budget is not None:
        conf["monthly_budget"] = float(args.budget)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    inbox = os.path.join(INBOX_DIR, stamp)
    os.makedirs(inbox, exist_ok=True)

    # ① レポートのダウンロード
    if args.input:
        inbox = args.input
        log(f"入力フォルダ: {inbox}（ダウンロードは省略）")
    else:
        try:
            import rms_steps
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                ctx = _browser(p, headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if not is_logged_in(page):
                    ctx.close()
                    log("ログインが切れているため中止しました。py rms_bot.py login で再ログインしてください")
                    return 2
                rms_steps.download_reports(page, inbox)
                ctx.close()
        except NotImplementedError as e:
            log(f"レポートの自動ダウンロードは未対応です: {e}")
            log(f"RMSからダウンロードしたCSVを {inbox} に置き、py rms_bot.py run --input \"{inbox}\" で実行してください")
            return 3

    # ② 運用エンジン
    result = run_once(inbox, OUT_DIR, conf, history_dir=OUT_DIR)
    log("アクションプランを作成しました: " + json.dumps(result["概要"], ensure_ascii=False, default=str))
    for k, v in result["出力"].items():
        log(f"  出力: {v}")

    # ③ RMSへの反映
    if not args.apply:
        log("--apply が無いため、RMSへの反映は行いません（CSVの作成まで）")
        return 0
    n = int(result["概要"].get("アクション数") or 0)
    if n > MAX_ACTIONS_PER_RUN:
        log(f"変更件数 {n} 件が上限 {MAX_ACTIONS_PER_RUN} 件を超えるため、自動反映を中止しました。内容を確認してください")
        return 4
    try:
        import rms_steps
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = _browser(p, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if not is_logged_in(page):
                ctx.close()
                log("ログインが切れているため反映を中止しました")
                return 2
            rms_steps.apply(page, result["出力"], conf)
            ctx.close()
        log("RMSへの反映が完了しました")
    except NotImplementedError as e:
        log(f"RMSへの自動反映は未対応です: {e}")
        return 3
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RMS RPP 自動運用ボット")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="手動でログインし、ログイン状態を保存する")
    sub.add_parser("check", help="ログイン状態を確認する")
    sub.add_parser("record", help="RMSでの操作を記録する")
    r = sub.add_parser("run", help="運用を1回実行する")
    r.add_argument("--apply", action="store_true", help="RMSへの反映まで行う")
    r.add_argument("--input", help="レポートCSVのフォルダ（指定するとダウンロードを省略）")
    r.add_argument("--budget", type=float, help="今回だけ月予算を変える（円）")
    args = ap.parse_args()
    return {"login": cmd_login, "check": cmd_check, "record": cmd_record, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
