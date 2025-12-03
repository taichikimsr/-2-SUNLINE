import re
import pdfplumber
from typing import Dict, Any, Optional
from datetime import datetime

def extract_text_from_pdf(pdf_path: str) -> str:
    """PDFからテキストを抽出"""
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text

def extract_tables_from_pdf(pdf_path: str) -> list:
    """PDFからテーブルを抽出"""
    all_tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if table:
                    all_tables.extend(table)
    return all_tables

def find_value_in_tables(tables: list, key_patterns: list) -> Optional[str]:
    """テーブルからキーに対応する値を検索"""
    for row in tables:
        if not row or len(row) < 2:
            continue
        cell0 = str(row[0]) if row[0] else ""
        for pattern in key_patterns:
            if pattern in cell0:
                # 次のセルが値
                for cell in row[1:]:
                    if cell:
                        return str(cell)
    return None

def parse_date(text: str) -> Optional[str]:
    """日付を抽出してYYYY-MM-DD形式に変換"""
    # 令和形式
    reiwa_match = re.search(r'令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日', text)
    if reiwa_match:
        year = 2018 + int(reiwa_match.group(1))
        month = int(reiwa_match.group(2))
        day = int(reiwa_match.group(3))
        return f"{year}-{month:02d}-{day:02d}"
    
    # 西暦形式
    western_match = re.search(r'(\d{4})\s*年\s*(\d+)\s*月\s*(\d+)\s*日', text)
    if western_match:
        return f"{western_match.group(1)}-{int(western_match.group(2)):02d}-{int(western_match.group(3)):02d}"
    
    return None

def extract_amount(text: str) -> Optional[int]:
    """金額を抽出（カンマ、円を除去して数値に変換）"""
    if not text:
        return None
    # 最初に出現する金額パターンを抽出
    amount_match = re.search(r'([0-9,，]+)\s*円', str(text))
    if amount_match:
        amount_str = re.sub(r'[,，]', '', amount_match.group(1))
        return int(amount_str)
    # 円がない場合はカンマ区切りの数字を探す
    amount_match = re.search(r'([0-9,，]+)', str(text))
    if amount_match:
        amount_str = re.sub(r'[,，]', '', amount_match.group(1))
        if len(amount_str) >= 4:  # 4桁以上の数字のみ
            return int(amount_str)
    return None

def parse_contract_pdf(pdf_path: str) -> Dict[str, Any]:
    """さんなび契約書PDFを解析して情報を抽出"""
    text = extract_text_from_pdf(pdf_path)
    tables = extract_tables_from_pdf(pdf_path)
    
    result = {
        '日付': None,
        '担当': None,
        '顧客名': None,
        '初回入金額': None,
        '2回目以降入金額': None,
        '契約開始日': None,
        '契約期間': None,
        '金額税込み': None,
        '金額税抜き': None,
        '初回入金日': None,
        '支払い回数': None,
    }
    
    # 日付（契約日）
    contract_date_match = re.search(r'契約日[：\s]*(\d{4}\s*年\s*\d+\s*月\s*\d+\s*日)', text)
    if contract_date_match:
        result['日付'] = parse_date(contract_date_match.group(1))
    else:
        header_date = re.search(r'(令和\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日)', text[:200])
        if header_date:
            result['日付'] = parse_date(header_date.group(1))
    
    # 担当
    tanto_match = re.search(r'(?:営業)?担当[：\s]*([^\n\r《》]+?)(?:\n|$|《)', text)
    if tanto_match:
        result['担当'] = tanto_match.group(1).strip()
    
    # 顧客名（ご契約者名）- テーブルから優先
    customer_from_table = find_value_in_tables(tables, ['ご契約者名'])
    if customer_from_table:
        result['顧客名'] = customer_from_table.strip()
    else:
        customer_patterns = [
            r'ご契約者名[：\s]*([^\n\r]+?)(?:\n|住所|所在地)',
            r'ご契約者名\s+([^\n\r]+)',
        ]
        for pattern in customer_patterns:
            match = re.search(pattern, text)
            if match:
                result['顧客名'] = match.group(1).strip()
                break
    
    # 初回入金額 - テーブルから優先
    first_payment_from_table = find_value_in_tables(tables, ['初回ご入金額', '初回入金額', '入金金額'])
    if first_payment_from_table:
        result['初回入金額'] = extract_amount(first_payment_from_table)
    else:
        first_payment_patterns = [
            r'初回ご入金額[：\s]*([0-9,，]+)\s*円',
            r'初回入金額[：\s]*([0-9,，]+)\s*円',
            r'入金金額[：\s]*([0-9,，]+)\s*円',
        ]
        for pattern in first_payment_patterns:
            match = re.search(pattern, text)
            if match:
                result['初回入金額'] = extract_amount(match.group(1))
                break
    
    # 2回目以降入金額（分割代金/月額）- テーブルから優先
    monthly_from_table = find_value_in_tables(tables, ['分割代金', '1回あたり'])
    if monthly_from_table:
        result['2回目以降入金額'] = extract_amount(monthly_from_table)
    else:
        monthly_patterns = [
            r'分割代金[：\s]*([0-9,，]+)\s*円',
            r'1\s*回あたり[：\s]*([0-9,，]+)\s*円',
            r'毎月の月額換算支払い金額[：\s\n]*([0-9,，]+)\s*円',
        ]
        for pattern in monthly_patterns:
            match = re.search(pattern, text)
            if match:
                result['2回目以降入金額'] = extract_amount(match.group(1))
                break
    
    # 契約開始日 - テーブルから優先
    start_from_table = find_value_in_tables(tables, ['サポート開始日', '対策開始日', '契約開始日'])
    if start_from_table:
        result['契約開始日'] = parse_date(start_from_table)
    else:
        start_patterns = [
            r'(?:サポート開始日|対策開始日|契約開始日)[：\s]*(\d{4}\s*年\s*\d+\s*月\s*\d+\s*日)',
            r'契約期間[：\s]*(\d{4}\s*年\s*\d+\s*月\s*\d+\s*日)\s*から',
        ]
        for pattern in start_patterns:
            match = re.search(pattern, text)
            if match:
                result['契約開始日'] = parse_date(match.group(1))
                break
    
    # 契約期間 - テーブルから優先
    period_from_table = find_value_in_tables(tables, ['契約期間'])
    if period_from_table:
        period_match = re.search(r'(\d+\s*(?:年間|ヶ月間|ヵ月間|か月間))', period_from_table)
        if period_match:
            result['契約期間'] = period_match.group(1).replace(' ', '')
    
    if not result['契約期間']:
        period_patterns = [
            r'契約期間[：\s]*(?:[^\d]*から)?(\d+\s*(?:年間|ヶ月間|ヵ月間|か月間))',
            r'契約期間[：\s]*楽天[^\d]*(\d+\s*(?:年間|ヶ月間))',
            r'から(\d+\s*(?:年間|ヶ月間|ヵ月間|か月間))',
        ]
        for pattern in period_patterns:
            match = re.search(pattern, text)
            if match:
                result['契約期間'] = match.group(1).replace(' ', '')
                break
    
    # 金額税込み（総額）- テーブルから優先
    total_from_table = find_value_in_tables(tables, ['利用料', '料金'])
    if total_from_table:
        result['金額税込み'] = extract_amount(total_from_table)
    else:
        total_patterns = [
            r'(?:利用料|料金|総支払い額)[：\s]*([0-9,，]+)\s*円[（\(]?税込',
            r'(?:利用料|料金|総支払い額)[：\s\n]*([0-9,，]+)\s*円',
            r'(\d+)回分総支払い額[：\s\n]*([0-9,，]+)\s*円',
        ]
        for pattern in total_patterns:
            match = re.search(pattern, text)
            if match:
                groups = match.groups()
                amount_str = groups[-1] if len(groups) > 1 else groups[0]
                result['金額税込み'] = extract_amount(amount_str)
                break
    
    # 金額税抜き（税込みから計算）
    if result['金額税込み']:
        result['金額税抜き'] = int(result['金額税込み'] / 1.1)
    
    # 初回入金日 - テーブルから優先
    first_date_from_table = find_value_in_tables(tables, ['初回入金日', '入金予定日'])
    if first_date_from_table:
        result['初回入金日'] = parse_date(first_date_from_table)
    else:
        first_date_patterns = [
            r'初回入金日[：\s]*(\d{4}\s*年\s*\d+\s*月\s*\d+\s*日)',
            r'入金予定日[：\s]*(\d{4}\s*年\s*\d+\s*月\s*\d+\s*日)',
        ]
        for pattern in first_date_patterns:
            match = re.search(pattern, text)
            if match:
                result['初回入金日'] = parse_date(match.group(1))
                break
    
    # 支払い回数
    installment_match = re.search(r'[×x]\s*(\d+)\s*回', text)
    if installment_match:
        result['支払い回数'] = int(installment_match.group(1))
    elif result['金額税込み'] and result['2回目以降入金額']:
        result['支払い回数'] = result['金額税込み'] // result['2回目以降入金額']
    
    return result

def format_for_display(data: Dict[str, Any]) -> Dict[str, str]:
    """表示用にデータをフォーマット"""
    formatted = {}
    for key, value in data.items():
        if value is None:
            formatted[key] = '-'
        elif isinstance(value, int):
            formatted[key] = f"{value:,}"
        else:
            formatted[key] = str(value)
    return formatted
