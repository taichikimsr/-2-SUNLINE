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
「record」で実際の操作を1回記録し、記録したファイル（rms_recorded_steps.json）を開発側に渡すと、自動操作に組み込める。

使い方（Windows、rpp_optimizer フォルダで）:
    py -m pip install playwright
    （ブラウザはPCに入っている Google Chrome または Microsoft Edge を使うため、
      「py -m playwright install chromium」は不要）
    py rms_bot.py login     … ツール専用の普通のChromeが開くので手動でログイン（「信頼できる端末」に登録すると14日間コード不要）
    py rms_bot.py check     … ログイン状態の確認
    py rms_bot.py explore   … RPP関係の画面を自動で調べる（閲覧のみ）→ rms_site_map.json を開発側へ
    py rms_bot.py record    … RMSでの操作を記録（押したボタン・画面のみ。パスワード・入力文字は記録しない）
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
RECORD_PATH = os.path.join(BASE, "rms_recorded_steps.json")
SITE_MAP_PATH = os.path.join(BASE, "rms_site_map.json")
SITE_MAP_RPP_PATH = os.path.join(BASE, "rms_site_map_rpp.json")

# RMSのURL（画面が変わった場合はここを変更）
RMS_LOGIN_URL = "https://glogin.rms.rakuten.co.jp/"
RMS_MAIN_URL = "https://mainmenu.rms.rakuten.co.jp/"
# RPPの画面（2026年11月の調査で確認）
RPP_PREFIX = "https://ad.rms.rakuten.co.jp/rpp/"
RPP_TOP_URL = RPP_PREFIX + "top"
RPP_URLS = {
    "campaigns": RPP_PREFIX + "campaigns",   # キャンペーン（月予算など）
    "items": RPP_PREFIX + "items",           # 商品・キーワード設定
    "exclude": RPP_PREFIX + "exclude",       # 除外商品
    "reports": RPP_PREFIX + "reports",       # パフォーマンスレポート
    "download": RPP_PREFIX + "download",     # ダウンロード履歴
}

MAX_ACTIONS_PER_RUN = 300  # これを超える変更は自動では反映しない


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- ブラウザ
# RMSへのログインは、自動操作用ではない「普通のChrome/Edge」で行う。
#   - 自動操作用に起動したブラウザは「自動テストソフトウェアによって制御されています」と表示され、
#     シークレットウィンドウのような空の状態で始まるため、ログインが通りにくい場合がある
#   - そこで、ツール専用のプロフィール（.rms_profile）で普通のChromeを起動し、人が通常どおりログインする
#   - ツールは、そのChromeに後からつないで操作する（Chromeのリモートデバッグ機能。このPCの中だけで通信）
CDP_PORT = 9333
BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def _browser_exe() -> str:
    import shutil

    if os.environ.get("RMS_BOT_BROWSER"):
        return os.environ["RMS_BOT_BROWSER"]
    for x in BROWSER_CANDIDATES:
        if os.path.exists(x):
            return x
    for name in ("chrome", "google-chrome", "msedge", "microsoft-edge"):
        if shutil.which(name):
            return shutil.which(name)
    return ""


def _cdp_alive() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1):
            return True
    except OSError:
        return False


def open_normal_browser(url: str) -> None:
    """ツール専用プロフィールで普通のChrome/Edgeを起動する（既に起動していれば何もしない）"""
    import time

    if _cdp_alive():
        return
    exe = _browser_exe()
    if not exe:
        raise RuntimeError("Google Chrome も Microsoft Edge も見つかりません。どちらかをインストールしてください。")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [exe, f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check"]
    if os.environ.get("RMS_BOT_HEADLESS"):  # テスト用
        args += ["--headless=new", "--no-sandbox"]
    subprocess.Popen(args + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if _cdp_alive():
            return
        time.sleep(0.5)
    raise RuntimeError("ブラウザを起動できませんでした。開いている「RMS用」のChromeウィンドウをすべて閉じてから、もう一度実行してください。")


def _kill_profile_processes() -> None:
    """ツール専用プロフィール（.rms_profile）で動いているChrome/Edgeだけを強制終了する（Windows）。
    普段使いのChromeは別のプロフィールなので終了しない。"""
    if os.name != "nt":
        return
    ps = ("Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') "
          "-and $_.CommandLine -like '*.rms_profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue }")
    subprocess.call(["powershell", "-NoProfile", "-Command", ps], timeout=60)


def close_normal_browser() -> bool:
    """ツール専用プロフィールのChromeを終了する（ウィンドウを閉じても裏で動き続けている場合があるため）"""
    import threading
    import time

    if not _cdp_alive():
        _kill_profile_processes()
        time.sleep(1)
        return True

    def _graceful():
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}", timeout=10_000)
                try:
                    browser.new_browser_cdp_session().send("Browser.close")
                except Exception:  # noqa: BLE001  終了の途中で接続が切れるのは正常
                    pass
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=_graceful, daemon=True)
    t.start()
    t.join(15)  # 15秒で応答が無ければ強制終了へ
    for _ in range(20):
        if not _cdp_alive():
            break
        time.sleep(0.5)
    if _cdp_alive():
        _kill_profile_processes()
        time.sleep(2)
    if not _cdp_alive():
        _kill_profile_processes()  # 裏に残ったプロセスも念のため終了
        time.sleep(2)  # プロフィールのロック解除を待つ
        return True
    return False


def connect(p, url: str = None):
    """専用プロフィールのChromeにつなぐ。戻り値: (context, page)"""
    open_normal_browser(url or RMS_MAIN_URL)
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def is_logged_in(page) -> bool:
    page.goto(RMS_MAIN_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2000)
    # ログインしていなければ、ログイン画面（glogin.rms.rakuten.co.jp）へ移動する
    return "glogin" not in page.url


def cmd_login(_args) -> int:
    from playwright.sync_api import sync_playwright

    # ログイン中はツールをつながず、普通のブラウザとして人が操作する
    open_normal_browser(RMS_LOGIN_URL)
    print("開いたChrome（またはEdge）で、普段どおりRMSにログインしてください。")
    print("  ・R-Login ID／楽天会員ID／パスワードは、このブラウザの画面に入力します（この黒い画面には入力しません）")
    print("  ・Viberの認証コードを入力し、「信頼できる端末」に登録すると14日間はコード不要になります")
    input("RMSのメインメニューが表示されたら、この画面で Enter を押してください … ")
    with sync_playwright() as p:
        _, page = connect(p)
        ok = is_logged_in(page)
    log("ログインを確認しました。このChromeのウィンドウは開いたままで構いません"
        if ok else "ログインを確認できませんでした。ブラウザでログインしてから、もう一度 login を実行してください")
    return 0 if ok else 1


def cmd_check(_args) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        _, page = connect(p)
        ok = is_logged_in(page)
    log("ログイン中です" if ok else "ログインが切れています。py rms_bot.py login で再ログインしてください")
    return 0 if ok else 2


def cmd_record(_args) -> int:
    from rms_recorder import record

    print("ログイン用と同じ普通のChromeで、操作を記録します。")
    print("  ※パスワードや、文字入力欄に入力した内容は記録しません（押したボタン・移動した画面・選んだファイル名のみ）")
    print("次の操作を、普段どおり1回ずつ行ってください:")
    print("  1. RPPのパフォーマンスレポートで、キーワード別（直近30日）をCSVダウンロード")
    print("  2. 同じく商品別をCSVダウンロード")
    print("  3. 商品・キーワード設定 → 一括アップロード（キーワードCPC）でCSVを選んで送信 ※テスト用の1行のCSVで")
    print("  4. キャンペーン設定で月予算を変更する画面を開く（保存はしなくて構いません）")
    print("ログイン画面が出た場合は、そのままログインしてください（入力内容は記録されません）。")
    return record(connect, RECORD_PATH, RMS_MAIN_URL)


def cmd_explore(args) -> int:
    from rms_explorer import explore

    print("RMSのRPP（広告）関係の画面を自動で調べます（閲覧のみ。送信・登録・削除・ダウンロードなどは押しません）。")
    print("Chromeが自動で画面を移動します。終わるまで操作せずにお待ちください（数分かかります）。")
    if args.rpp:
        # RPPの画面（キャンペーン・商品・キーワード設定・除外商品・パフォーマンスレポート）だけを詳しく調べる
        return explore(connect, SITE_MAP_RPP_PATH, RPP_TOP_URL, max_pages=args.max_pages,
                       prefix=RPP_PREFIX, max_depth=3)
    return explore(connect, SITE_MAP_PATH, RMS_MAIN_URL, max_pages=args.max_pages)


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
                _, page = connect(p)
                if not is_logged_in(page):
                    log("ログインが切れているため中止しました。py rms_bot.py login で再ログインしてください")
                    return 2
                rms_steps.download_reports(page, inbox)
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
            _, page = connect(p)
            if not is_logged_in(page):
                log("ログインが切れているため反映を中止しました")
                return 2
            rms_steps.apply(page, result["出力"], conf)
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
    ex = sub.add_parser("explore", help="RMSのRPP関係の画面を自動で調べる（閲覧のみ）")
    ex.add_argument("--max-pages", type=int, default=40)
    ex.add_argument("--rpp", action="store_true", help="RPPの画面だけを詳しく調べる")
    r = sub.add_parser("run", help="運用を1回実行する")
    r.add_argument("--apply", action="store_true", help="RMSへの反映まで行う")
    r.add_argument("--input", help="レポートCSVのフォルダ（指定するとダウンロードを省略）")
    r.add_argument("--budget", type=float, help="今回だけ月予算を変える（円）")
    args = ap.parse_args()
    return {"login": cmd_login, "check": cmd_check, "record": cmd_record, "explore": cmd_explore,
            "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
