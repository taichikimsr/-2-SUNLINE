import gspread
from google.oauth2.service_account import Credentials
from typing import Dict, Any, List, Optional
import json
from datetime import datetime
from dateutil.relativedelta import relativedelta

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# スプレッドシートの列構成（2行目がヘッダー）
# A: No., B: 管理番号, C: 日付, D: 担当, E: 顧客名（リンク）, F: 制作
# G: 初回入金額, H: 2回目以降, I: 開始, J: 期間, K: 終了日, L: 数
# M: 税込み, N: 税抜き, O: 初回入金日, P: 支払い回数, Q: 口座振替依頼書, R: 支払い方法

def get_gspread_client(credentials_json: str) -> gspread.Client:
    """認証情報からgspreadクライアントを取得"""
    creds_dict = json.loads(credentials_json)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(credentials)

def calculate_end_date(start_date: str, period: str) -> Optional[str]:
    """契約開始日と期間から終了日を計算"""
    if not start_date or not period:
        return None
    try:
        start = datetime.strptime(start_date, '%Y-%m-%d')
        import re
        year_match = re.search(r'(\d+)\s*年', period)
        month_match = re.search(r'(\d+)\s*(?:ヶ月|ヵ月|か月)', period)
        
        years = int(year_match.group(1)) if year_match else 0
        months = int(month_match.group(1)) if month_match else 0
        
        end = start + relativedelta(years=years, months=months)
        return end.strftime('%Y/%m/%d')
    except:
        return None

def extract_period_months(period: str) -> Optional[int]:
    """期間から月数を抽出"""
    if not period:
        return None
    import re
    year_match = re.search(r'(\d+)\s*年', str(period))
    month_match = re.search(r'(\d+)\s*(?:ヶ月|ヵ月|か月)', str(period))
    
    months = 0
    if year_match:
        months += int(year_match.group(1)) * 12
    if month_match:
        months += int(month_match.group(1))
    return months if months > 0 else None

def convert_to_sheet_row(data: Dict[str, Any], row_num: int, management_num: str) -> List:
    """PDFから抽出したデータをスプレッドシートの行形式に変換"""
    # 終了日を計算
    end_date = calculate_end_date(data.get('契約開始日'), data.get('契約期間'))
    
    # 期間を月数に変換
    period_months = extract_period_months(data.get('契約期間'))
    
    # 日付フォーマット変換 (YYYY-MM-DD → YYYY/MM/DD)
    def format_date(d):
        if d and '-' in str(d):
            return str(d).replace('-', '/')
        return d
    
    # スプレッドシートの列順に合わせる（A〜R）
    row = [
        row_num,                                    # A: No.
        management_num,                             # B: 管理番号
        format_date(data.get('日付')) or '',       # C: 日付
        data.get('担当') or '',                    # D: 担当
        data.get('顧客名') or '',                  # E: 顧客名（リンク）
        '',                                         # F: 制作（空欄）
        data.get('初回入金額') or '',              # G: 初回入金額
        data.get('2回目以降入金額') or '',         # H: 2回目以降
        '前',                                       # I: 開始（前払いなので「前」）
        period_months or '',                        # J: 期間（月数）
        end_date or '',                            # K: 終了日
        data.get('支払い回数') or '',              # L: 数
        data.get('金額税込み') or '',              # M: 税込み
        data.get('金額税抜き') or '',              # N: 税抜き
        format_date(data.get('初回入金日')) or '', # O: 初回入金日
        f"{data.get('支払い回数') or ''}回" if data.get('支払い回数') else '',  # P: 支払い回数
        '',                                         # Q: 口座振替依頼書（空欄）
        '三井住友振込/アプラス',                   # R: 支払い方法
    ]
    return row

def add_borders(spreadsheet, worksheet, start_row: int, end_row: int, num_cols: int = 18):
    """追加した行に罫線を追加"""
    try:
        # Sheets APIを使用して罫線を追加
        requests = [{
            "updateBorders": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": start_row - 1,  # 0-indexed
                    "endRowIndex": end_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "top": {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}},
                "bottom": {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}},
                "left": {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}},
                "right": {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}},
                "innerHorizontal": {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}},
                "innerVertical": {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}}
            }
        }]
        spreadsheet.batch_update({"requests": requests})
        return True
    except Exception as e:
        print(f"罫線追加エラー: {e}")
        return False

def append_to_spreadsheet(
    credentials_json: str,
    spreadsheet_id: str,
    sheet_name: str,
    data_list: List[Dict[str, Any]]
) -> dict:
    """Googleスプレッドシートにデータを追加"""
    try:
        client = get_gspread_client(credentials_json)
        spreadsheet = client.open_by_key(spreadsheet_id)
        
        # シート取得（「売上台帳」を優先）
        worksheet = None
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            # 「売上台帳」がなければ最初のシートを使用
            worksheet = spreadsheet.sheet1
        
        # 既存データの最終行を取得
        all_values = worksheet.get_all_values()
        last_row = len(all_values)
        
        # 次のNo.と管理番号を計算（A列とB列の最大値+1）
        max_no = 0
        max_mgmt = 0
        for row in all_values[2:]:  # 3行目以降（データ行）
            try:
                no = int(row[0]) if row[0] else 0
                max_no = max(max_no, no)
            except:
                pass
            try:
                mgmt = int(row[1]) if row[1] else 0
                max_mgmt = max(max_mgmt, mgmt)
            except:
                pass
        
        # データを行形式に変換
        rows_to_add = []
        for i, data in enumerate(data_list):
            new_no = max_no + i + 1
            new_mgmt = str(max_mgmt + i + 1)
            row = convert_to_sheet_row(data, new_no, new_mgmt)
            rows_to_add.append(row)
        
        # データ追加
        start_row = last_row + 1
        if rows_to_add:
            range_start = f'A{start_row}'
            worksheet.update(range_start, rows_to_add, value_input_option='USER_ENTERED')
            
            # 罫線を追加
            end_row = start_row + len(rows_to_add) - 1
            add_borders(spreadsheet, worksheet, start_row, end_row + 1)
        
        return {
            'success': True,
            'message': f'{len(rows_to_add)}件のデータを追加しました（行 {start_row}〜{start_row + len(rows_to_add) - 1}）',
            'sheet_url': f"{spreadsheet.url}#gid={worksheet.id}"
        }
        
    except Exception as e:
        return {
            'success': False,
            'message': str(e)
        }

def test_connection(credentials_json: str, spreadsheet_id: str) -> dict:
    """接続テスト"""
    try:
        client = get_gspread_client(credentials_json)
        spreadsheet = client.open_by_key(spreadsheet_id)
        
        # シート一覧を取得
        sheets = [ws.title for ws in spreadsheet.worksheets()]
        
        return {
            'success': True,
            'message': f'接続成功: {spreadsheet.title}',
            'sheets': sheets,
            'sheet_url': spreadsheet.url
        }
    except Exception as e:
        return {
            'success': False,
            'message': f'接続失敗: {str(e)}'
        }
