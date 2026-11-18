"""RMS（RPP）の画面操作

2026年11月の画面調査（rms_site_map_rpp.json）で確認した要素を使う。
  パフォーマンスレポート: https://ad.rms.rakuten.co.jp/rpp/reports
    集計単位 #rdReportTypeKeyword / #rdReportTypeItem、期間 #rdPeriodAll、開始/終了の日付欄、
    指標のチェック #cbMetricsAll ほか、「この条件でダウンロード」#btnDownloadReport
  ダウンロード履歴: https://ad.rms.rakuten.co.jp/rpp/download（作成されたファイルを「ダウンロード」リンクから取得）
  除外商品: https://ad.rms.rakuten.co.jp/rpp/exclude
    「一括アップロード」#btnBulkUploadExcludeItemOpenModal → ファイル欄 input[type=file] →「アップロード」#btnUploadFile
  商品・キーワード設定: https://ad.rms.rakuten.co.jp/rpp/items
    「一括アップロード」#btnBulkUploadItemKeyword（押した後の画面は未確認 → probe で確認する）

まだ画面で確認できていない部分は、probe（お試し実行）で記録し、実際の反映前に確かめる。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import time
from datetime import date, datetime, timedelta

RPP = "https://ad.rms.rakuten.co.jp/rpp/"
WAIT = 2500


class StepError(RuntimeError):
    pass


# ---------------------------------------------------------------- 共通
def _goto(page, path):
    page.goto(RPP + path, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(WAIT)
    if "glogin" in page.url:
        raise StepError("ログインが切れています（py rms_bot.py login）")


def _snapshot(page, note=""):
    """画面の状態（見出し・ボタン・表示中のメッセージ）を記録用に集める。入力欄の値は集めない"""
    js = r"""
    () => {
      const clean = (s, n=120) => (s||"").replace(/\s+/g," ").trim().slice(0,n);
      const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      return {
        url: location.href,
        dialogs: Array.from(document.querySelectorAll('[role=dialog],.modal,[class*=Modal],[class*=modal]')).filter(vis)
                   .slice(0,5).map(d => clean(d.innerText, 600)),
        buttons: Array.from(document.querySelectorAll('button,[role=button],[role=menuitem],li a')).filter(vis)
                   .slice(0,80).map(b => ({text: clean(b.innerText||b.value,40), id: b.id||""})),
        files: Array.from(document.querySelectorAll('input[type=file]')).map(i => ({id:i.id, name:i.name, accept:i.accept})),
        alerts: Array.from(document.querySelectorAll('[role=alert],[class*=error],[class*=Error],[class*=success],[class*=toast]'))
                   .filter(vis).slice(0,10).map(a => clean(a.innerText, 300)),
      };
    }"""
    try:
        snap = page.evaluate(js)
    except Exception as e:  # noqa: BLE001
        snap = {"error": str(e)[:200]}
    snap["note"] = note
    snap["time"] = datetime.now().strftime("%H:%M:%S")
    return snap


def _set_checked(page, selector, value=True):
    loc = page.locator(selector)
    if loc.count() and loc.first.is_enabled():
        if loc.first.is_checked() != value:
            loc.first.click()


def _fill_date(page, which, value):
    """開始・終了の日付欄（Select start / Select end）に入力する"""
    sel = f'input[aria-label="Select {which}"], input[placeholder="Select {which}"]'
    loc = page.locator(sel)
    if not loc.count():
        return False
    loc.first.click()
    loc.first.fill(value)
    loc.first.press("Enter")
    page.wait_for_timeout(500)
    return True


def _user_downloads_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")


def _wait_download(page, click, dest_dir, started, pattern="rpp_*.csv", timeout_s=90):
    """ダウンロードを待って dest_dir に保存する。
    Playwright でダウンロードを受け取れない場合は、Chromeの保存先（ダウンロードフォルダ）に新しくできたファイルを拾う。"""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with page.expect_download(timeout=15_000) as dl:
            click()
        d = dl.value
        path = os.path.join(dest_dir, d.suggested_filename)
        d.save_as(path)
        return path
    except Exception:  # noqa: BLE001
        pass
    end = time.time() + timeout_s
    while time.time() < end:
        files = [f for f in glob.glob(os.path.join(_user_downloads_dir(), pattern))
                 if os.path.getmtime(f) >= started and not f.endswith(".crdownload")]
        if files:
            src = max(files, key=os.path.getmtime)
            time.sleep(1)
            path = os.path.join(dest_dir, os.path.basename(src))
            shutil.copy2(src, path)
            return path
        time.sleep(2)
    raise StepError("ダウンロードしたファイルが見つかりませんでした")


# ---------------------------------------------------------------- レポートのダウンロード
def request_report(page, kind: str, days: int = 30):
    """パフォーマンスレポートの作成を依頼する（kind: keyword / item）"""
    _goto(page, "reports")
    page.locator("#rdReportTypeKeyword" if kind == "keyword" else "#rdReportTypeItem").first.click()
    page.locator("#rdPeriodAll").first.click()  # 全期間で表示（＝期間の合計）
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    ok = _fill_date(page, "start", start.isoformat()) and _fill_date(page, "end", end.isoformat())
    for cb in ("#cbMetricsAll", "#cbAllUsers", "#cbNewUsers", "#cbExistingUsers", "#cb12H", "#cb720H"):
        _set_checked(page, cb, True)
    page.wait_for_timeout(500)
    page.locator("#btnDownloadReport").first.click()
    page.wait_for_timeout(WAIT)
    return {"kind": kind, "start": start.isoformat(), "end": end.isoformat(), "dates_filled": ok}


def _matches_request(path, want=None):
    """ダウンロードしたレポートが、依頼した集計単位・期間のものか確かめる"""
    if not want:
        return True
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from rpp.io import read_csv_bytes

        with open(path, "rb") as f:
            meta = " ".join(read_csv_bytes(f.read()).attrs.get("meta", []))
    except Exception:  # noqa: BLE001
        return False
    unit = "キーワード別" if want["kind"] == "keyword" else "商品別"
    return unit in meta and want["end"] in meta


def fetch_latest_download(page, dest_dir, started, retries=20, want=None):
    """ダウンロード履歴の一番上（最新）のファイルを、作成完了まで待って取得する。
    want を渡すと、依頼した集計単位・期間のファイルかを確かめ、違えば作成完了まで待ち直す。"""
    _goto(page, "download")
    for _ in range(retries):
        link = page.get_by_role("link", name="ダウンロード").first
        if link.count():
            try:
                path = _wait_download(page, link.click, dest_dir, started, timeout_s=20)
                if _matches_request(path, want):
                    return path
                os.remove(path)  # 前回のレポートだった → 作成完了を待つ
            except StepError:
                pass
        btn = page.locator("#btnDownloadHistoryRefresh")
        if btn.count():
            btn.first.click()
        page.wait_for_timeout(5000)
    raise StepError("レポートの作成が完了しませんでした（ダウンロード履歴を確認してください）")


def download_reports(page, dest_dir: str) -> None:
    """キーワード別・商品別レポート（直近30日）を dest_dir に保存する"""
    for kind in ("keyword", "item"):
        started = time.time()
        want = request_report(page, kind)
        fetch_latest_download(page, dest_dir, started, want=want)


# ---------------------------------------------------------------- 一括アップロード
def _upload_in_modal(page, file_path, submit: bool, log: list, label: str):
    upload_btn = page.locator("#btnUploadFile:visible")
    try:
        upload_btn.first.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        raise StepError(f"{label}: 一括アップロードの画面が開きませんでした")
    # 表示中の画面（ダイアログ）の中のファイル欄を使う（画面に隠れたファイル欄が複数ある場合があるため）
    files = page.locator("[role=dialog]:visible input[type=file], .modal:visible input[type=file], "
                         "#fuFileUploadBulk input[type=file]")
    if not files.count():
        files = page.locator("input[type=file]")
    if not files.count():
        raise StepError(f"{label}: ファイル選択欄が見つかりません")
    files.first.set_input_files(file_path)
    page.wait_for_timeout(1000)
    log.append(_snapshot(page, f"{label}: ファイルを選択"))
    if not submit:
        log.append({"note": f"{label}: お試し実行のためアップロードは押していません"})
        page.keyboard.press("Escape")
        return
    upload_btn.first.click()
    page.wait_for_timeout(5000)
    log.append(_snapshot(page, f"{label}: アップロード後"))


def upload_exclusions(page, file_path, submit, log):
    _goto(page, "exclude")
    page.locator("#btnBulkUploadExcludeItemOpenModal").first.click()
    page.wait_for_timeout(WAIT)
    log.append(_snapshot(page, "除外商品: 一括アップロードを開いた"))
    _upload_in_modal(page, file_path, submit, log, "除外商品")


def upload_cpc(page, file_path, kind, submit, log):
    """kind: keyword（キーワードCPC）/ item（商品CPC）"""
    _goto(page, "items")
    page.locator("#btnBulkUploadItemKeyword").first.click()
    page.wait_for_timeout(WAIT)
    log.append(_snapshot(page, "商品・キーワード: 一括アップロードを押した"))
    # 押した後にメニューが出る場合（「キーワードCPCの登録/更新」など）はそれを選ぶ
    word = "キーワードCPC" if kind == "keyword" else "商品CPC"
    if not page.locator("#btnUploadFile:visible").count():
        cands = page.get_by_text(word, exact=False)
        chosen = ""
        for i in range(min(cands.count(), 20)):
            el = cands.nth(i)
            try:
                t = el.inner_text()
                if el.is_visible() and ("登録" in t or "更新" in t or "アップロード" in t):
                    el.click()
                    chosen = t.strip()[:40]
                    page.wait_for_timeout(WAIT)
                    break
            except Exception:  # noqa: BLE001
                continue
        log.append(_snapshot(page, f"商品・キーワード: メニュー選択「{chosen or '見つからず'}」"))
    _upload_in_modal(page, file_path, submit, log, "キーワードCPC" if kind == "keyword" else "商品CPC")


def apply(page, outputs: dict, settings: dict, submit: bool = True) -> list:
    """除外商品 → 商品CPC → キーワードCPC の順に一括アップロードする。月予算はまだ自動化していない。"""
    log = []
    by = {k: v for k, v in outputs.items()}
    order = [("exclude_upload", upload_exclusions), ("item_upload", lambda pg, f, s, l: upload_cpc(pg, f, "item", s, l)),
             ("keyword_upload", lambda pg, f, s, l: upload_cpc(pg, f, "keyword", s, l))]
    for key, fn in order:
        path = next((v for k, v in by.items() if key in k), None)
        if path:
            try:
                fn(page, path, submit, log)
            except Exception as e:  # noqa: BLE001
                log.append(_snapshot(page, f"エラー: {key}: {e}"))
                if submit:
                    raise
    return log


def read_campaigns(page) -> list:
    """キャンペーン一覧の表の文字を読む（配信タイプ・継続月予算・CPC の確認用）"""
    _goto(page, "campaigns")
    try:
        return page.evaluate(r"""() => Array.from(document.querySelectorAll('table tbody tr')).slice(0, 20)
            .map(tr => Array.from(tr.querySelectorAll('td')).map(td => (td.innerText||'').replace(/\s+/g,' ').trim().slice(0,60)))""")
    except Exception as e:  # noqa: BLE001
        return [[f"読み取りエラー: {e}"]]


def probe(page, out_path, sample_files: dict, dest_dir: str) -> dict:
    """お試し実行: レポートを実際にダウンロードし、一括アップロードはファイル選択まで（アップロードは押さない）"""
    result = {"time": datetime.now().isoformat(timespec="seconds"), "steps": []}
    st = result["steps"]
    try:
        result["campaigns"] = read_campaigns(page)
    except Exception as e:  # noqa: BLE001
        st.append({"note": f"キャンペーン読み取りエラー: {e}"})
    for kind in ("keyword", "item"):
        try:
            started = time.time()
            info = request_report(page, kind)
            st.append({"note": f"レポート依頼 {kind}", **info})
            st.append(_snapshot(page, f"レポート依頼後 {kind}"))
            path = fetch_latest_download(page, dest_dir, started, want=info)
            st.append({"note": f"レポート取得 {kind}", "file": os.path.basename(path)})
        except Exception as e:  # noqa: BLE001
            st.append(_snapshot(page, f"レポート取得エラー {kind}: {e}"))
    try:
        st.extend(apply(page, sample_files, {}, submit=False))
    except Exception as e:  # noqa: BLE001
        st.append({"note": f"一括アップロード確認エラー: {e}"})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return result
