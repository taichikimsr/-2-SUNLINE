# さんなび契約書解析ツール

PDFからさんなび契約書のデータを自動抽出し、Googleスプレッドシート（売上台帳）に追加するWebアプリです。

## 機能

- **PDF解析**: 契約書PDFから各項目を自動抽出
- **スプレッドシート連携**: 売上台帳シートの最下部に自動追記
- **罫線付き**: 追加したデータに自動で罫線を適用

## セットアップ

```bash
# 依存関係インストール
pip install -r requirements.txt

# アプリ起動
streamlit run app.py
```

ブラウザで http://localhost:8501 にアクセス

## ファイル構成

```
sannabi-parser/
├── app.py                           # メインアプリ
├── pdf_parser.py                    # PDF解析モジュール
├── google_sheets.py                 # Googleスプレッドシート連携
├── keiyakusho-1e1bbb4afd65.json    # サービスアカウント認証情報
├── requirements.txt                 # 依存関係
└── .streamlit/
    └── config.toml                  # Streamlit設定
```

## デフォルト設定

- スプレッドシートID: `1poD5gsxCnMrpZrKNd8diJUv_IjgS3K72427Pj-ROqTE`
- シート名: `売上台帳`
- 認証ファイル: `keiyakusho-1e1bbb4afd65.json`（同梱）

## 出力列構成

| 列 | 項目 | 自動抽出 |
|----|------|----------|
| A | No. | ✅ 自動連番 |
| B | 管理番号 | ✅ 自動連番 |
| C | 日付 | ✅ |
| D | 担当 | ✅ |
| E | 顧客名 | ✅ |
| F | 制作 | - |
| G | 初回入金額 | ✅ |
| H | 2回目以降 | ✅ |
| I | 開始 | 「前」（前払い） |
| J | 期間 | ✅（月数） |
| K | 終了日 | ✅（自動計算） |
| L | 数 | ✅ |
| M | 税込み | ✅ |
| N | 税抜き | ✅ |
| O | 初回入金日 | ✅ |
| P | 支払い回数 | ✅ |
| Q | 口座振替依頼書 | - |
| R | 支払い方法 | デフォルト値 |

---
SUNLINE合同会社
