import streamlit as st
import pandas as pd
from datetime import datetime
import tempfile
import os

from pdf_parser import parse_contract_pdf
from google_sheets import append_to_spreadsheet, test_connection

# ページ設定
st.set_page_config(
    page_title="さんなび契約書解析ツール",
    page_icon="📄",
    layout="wide"
)

# カスタムCSS
st.markdown("""
<style>
    .main-header {
        font-size: 2rem;
        font-weight: bold;
        color: #FF6B00;
        margin-bottom: 1rem;
    }
    .sub-header {
        font-size: 1.2rem;
        color: #666;
        margin-bottom: 2rem;
    }
</style>
""", unsafe_allow_html=True)

# タイトル
st.markdown('<div class="main-header">📄 さんなび契約書解析ツール</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">PDFからデータを抽出してGoogleスプレッドシートに追加</div>', unsafe_allow_html=True)

# 使い方説明
with st.expander("📖 使い方", expanded=False):
    st.markdown("""
    1. さんなび契約書PDFファイルをアップロード
    2. 「契約書を解析する」ボタンをクリック
    3. 解析結果を確認
    4. 「スプレッドシートに送信」ボタンをクリック
    5. 売上台帳シートの最下部に自動追加されます
    """)

# セッション状態初期化
if 'parsed_data' not in st.session_state:
    st.session_state.parsed_data = []

# サイドバー - Googleスプレッドシート設定
st.sidebar.markdown("## 🔧 Googleスプレッドシート設定")

credentials_json = None
spreadsheet_id = "1poD5gsxCnMrpZrKNd8diJUv_IjgS3K72427Pj-ROqTE"
sheet_name = "売上台帳"

# JSONファイルの読み込み方法を選択
json_source = st.sidebar.radio(
    "認証情報の読み込み方法",
    ["ファイルから読み込み", "アップロード"],
    index=0
)

if json_source == "ファイルから読み込み":
    json_path = st.sidebar.text_input(
        "JSONファイルパス",
        value="keiyakusho-1e1bbb4afd65.json",
        help="サービスアカウントのJSONファイルパス（app.pyと同じディレクトリ）"
    )
    if json_path:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(script_dir, json_path)
        if os.path.exists(full_path):
            with open(full_path, 'r') as f:
                credentials_json = f.read()
            st.sidebar.success("✅ JSONファイル読み込み完了")
        else:
            st.sidebar.warning(f"⚠️ ファイルが見つかりません: {json_path}")
else:
    credentials_file = st.sidebar.file_uploader(
        "サービスアカウントJSON",
        type=['json'],
        help="Google Cloud Consoleからダウンロードしたサービスアカウントのキーファイル"
    )
    if credentials_file:
        credentials_json = credentials_file.read().decode('utf-8')

spreadsheet_id = st.sidebar.text_input(
    "スプレッドシートID",
    value="1poD5gsxCnMrpZrKNd8diJUv_IjgS3K72427Pj-ROqTE",
    help="スプレッドシートURLの /d/ と /edit の間の文字列"
)

sheet_name = st.sidebar.text_input(
    "シート名",
    value="売上台帳",
    help="データを追加するシート名"
)

# 接続テストボタン
if st.sidebar.button("🔗 接続テスト"):
    if credentials_json and spreadsheet_id:
        result = test_connection(credentials_json, spreadsheet_id)
        if result['success']:
            st.sidebar.success(result['message'])
            if 'sheets' in result:
                st.sidebar.info(f"シート: {', '.join(result['sheets'])}")
        else:
            st.sidebar.error(result['message'])
    else:
        st.sidebar.warning("認証情報とスプレッドシートIDを入力してください")

# メインエリア
st.markdown("### 📄 契約書PDFファイル")
pdf_files = st.file_uploader(
    "さんなび契約書PDFファイルを選択してください",
    type=['pdf'],
    accept_multiple_files=True,
    help="複数ファイル選択可能"
)

st.markdown("---")

# 解析ボタン
if st.button("🔍 契約書を解析する", type="primary", use_container_width=True):
    if not pdf_files:
        st.error("PDFファイルをアップロードしてください")
    else:
        parsed_results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for idx, pdf_file in enumerate(pdf_files):
            status_text.text(f"解析中... {pdf_file.name} ({idx + 1}/{len(pdf_files)})")
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                tmp.write(pdf_file.read())
                tmp_path = tmp.name
            
            try:
                result = parse_contract_pdf(tmp_path)
                result['_filename'] = pdf_file.name
                parsed_results.append(result)
            except Exception as e:
                st.error(f"エラー ({pdf_file.name}): {str(e)}")
            finally:
                os.unlink(tmp_path)
            
            progress_bar.progress((idx + 1) / len(pdf_files))
        
        status_text.text("解析完了！")
        st.session_state.parsed_data = parsed_results

# 解析結果表示
if st.session_state.parsed_data:
    st.markdown("### 📊 解析結果")
    
    # 表示用カラム
    display_columns = ['日付', '担当', '顧客名', '初回入金額', '2回目以降入金額', 
                       '契約開始日', '契約期間', '金額税込み', '金額税抜き', 
                       '初回入金日', '支払い回数', 'ファイル名']
    
    display_data = []
    for item in st.session_state.parsed_data:
        row = {
            '日付': item.get('日付'),
            '担当': item.get('担当'),
            '顧客名': item.get('顧客名'),
            '初回入金額': item.get('初回入金額'),
            '2回目以降入金額': item.get('2回目以降入金額'),
            '契約開始日': item.get('契約開始日'),
            '契約期間': item.get('契約期間'),
            '金額税込み': item.get('金額税込み'),
            '金額税抜き': item.get('金額税抜き'),
            '初回入金日': item.get('初回入金日'),
            '支払い回数': item.get('支払い回数'),
            'ファイル名': item.get('_filename', '-')
        }
        display_data.append(row)
    
    df = pd.DataFrame(display_data)
    
    # 表示（編集可能）
    edited_df = st.data_editor(
        df,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "初回入金額": st.column_config.NumberColumn(format="%d"),
            "2回目以降入金額": st.column_config.NumberColumn(format="%d"),
            "金額税込み": st.column_config.NumberColumn(format="%d"),
            "金額税抜き": st.column_config.NumberColumn(format="%d"),
            "支払い回数": st.column_config.NumberColumn(format="%d"),
        }
    )
    
    st.markdown("---")
    
    # 出力ボタン
    col_a, col_b = st.columns(2)
    
    with col_a:
        if credentials_json and spreadsheet_id:
            if st.button("🚀 スプレッドシートに送信", type="primary", use_container_width=True):
                # 編集されたデータを元の形式に変換
                export_data = []
                for _, row in edited_df.iterrows():
                    export_data.append({
                        '日付': row.get('日付'),
                        '担当': row.get('担当'),
                        '顧客名': row.get('顧客名'),
                        '初回入金額': row.get('初回入金額'),
                        '2回目以降入金額': row.get('2回目以降入金額'),
                        '契約開始日': row.get('契約開始日'),
                        '契約期間': row.get('契約期間'),
                        '金額税込み': row.get('金額税込み'),
                        '金額税抜き': row.get('金額税抜き'),
                        '初回入金日': row.get('初回入金日'),
                        '支払い回数': row.get('支払い回数'),
                    })
                
                with st.spinner("送信中..."):
                    result = append_to_spreadsheet(
                        credentials_json,
                        spreadsheet_id,
                        sheet_name,
                        export_data
                    )
                
                if result['success']:
                    st.success(result['message'])
                    st.markdown(f"[📊 スプレッドシートを開く]({result['sheet_url']})")
                else:
                    st.error(result['message'])
        else:
            st.warning("サイドバーでGoogleスプレッドシートの設定を完了してください")
    
    with col_b:
        if st.button("🗑️ 結果をクリア", use_container_width=True):
            st.session_state.parsed_data = []
            st.rerun()

# フッター
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #888; font-size: 0.8rem;'>"
    "さんなび契約書解析ツール v1.1 | SUNLINE合同会社"
    "</div>",
    unsafe_allow_html=True
)
