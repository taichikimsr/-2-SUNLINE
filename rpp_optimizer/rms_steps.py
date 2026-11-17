"""RMSの画面操作（ボタンの位置など）

RMSの画面構成をまだ記録できていないため、未実装。
`py rms_bot.py record` で記録した rms_recorded_steps.py をもとに、ここへ組み込む。
"""


def download_reports(page, dest_dir: str) -> None:
    """RPPのキーワード別・商品別レポート（直近30日）を dest_dir にダウンロードする"""
    raise NotImplementedError("RMSのレポート画面の操作が未記録です（py rms_bot.py record）")


def apply(page, outputs: dict, settings: dict) -> None:
    """除外商品・商品CPC・キーワードCPCの一括アップロードと、月予算の設定を行う"""
    raise NotImplementedError("RMSの一括アップロード画面の操作が未記録です（py rms_bot.py record）")
