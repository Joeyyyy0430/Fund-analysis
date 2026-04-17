import pandas as pd
import os
import re

from trade_ledger import get_db_path, upsert_source_transactions

# --- Configuration ---
INPUT_FILE = 'data.xlsx'
OUTPUT_FILE = 'fund_data/transactions.csv'

# Names to exclude (partial match)
EXCLUDE_KEYWORDS = [
    "中欧医疗健康",
    "永赢先进制造"
]

# Mapping Name -> Code
# Based on existing fund_app.py and transactions.csv inspection
NAME_TO_CODE_MAP = {
    "华夏中证人工智能主题 ETF联接 C": "008586",
    "华夏人工智能 ETF联接 C": "008586",
    "华夏中证人工智能主题 ETF联接": "008586", # Generic fallback
    
    "国泰国证有色金属行业指数 C": "015596",
    "国泰国证有色金属行业指数C": "015596",
    
    "广发纳斯达克 100ETF联接 (QDII)C": "006479",
    "广发纳斯达克": "006479", # Shortened
    
    "易方达中证新能源 ETF联接 C": "019316",
    "易方达新能源 ETF联接 C": "019316",
    
    "汇丰晋信低碳先锋股票C": "013511",
    
    "国泰恒生电网 ETF联接 C": "023639",
    
    "华安国证航天航空行业 ETF联接 C": "025733",
    "华安国证航天航空行业 ETF联接": "025733",
    
    "南方中证半导体产业指数C": "020840",
    
    "富国中证细分化工产业主题 ETF联接 C": "020274",
    
    "永赢国证商用卫星通信产业 ETF联接 C": "024195",
    
    "融通中证云计算与大数据主题指数 (LOF)C": "014130",
    
    "富国新兴产业股票 C": "015686",
    
    "德邦稳盈增长灵活配置混合 C": "018463",
    
    "国泰黄金 ETF联接 C": "004253",
}

def normalize_date(date_obj):
    """
    Parses date from Excel (Timestamp) or string format.
    Returns YYYY-MM-DD string.
    """
    try:
        return pd.to_datetime(date_obj).strftime('%Y-%m-%d')
    except:
        return str(date_obj)

def map_trans_type(tx_dir):
    if "买入" in str(tx_dir) or "转换" in str(tx_dir):
        return "BUY"
    elif "卖出" in str(tx_dir):
        return "SELL"
    return "UNKNOWN"

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found.")
        return

    print(f"Reading {INPUT_FILE}...")
    try:
        df = pd.read_excel(INPUT_FILE)
    except Exception as e:
        print(f"Failed to read Excel: {e}")
        return

    # Clean header columns stripped of whitespace
    df.columns = df.columns.str.strip()
    
    print("DEBUG Columns:", df.columns.tolist())
    if not df.empty:
        print("DEBUG First Row:", df.iloc[0].to_dict())

    valid_rows = []

    for index, row in df.iterrows():

        # 1. Get Product Name
        p_name = str(row.get('基金名称', ''))
        # Clean name: remove newlines, extra spaces
        p_name = p_name.replace('\n', '').strip()
        p_name = re.sub(r'\s+', ' ', p_name)
        
        # 2. Filter Exclusions
        if any(ex in p_name for ex in EXCLUDE_KEYWORDS):
            continue
            
        # 3. Map Code
        # First try the '基金代码' column
        raw_code = row.get('基金代码')
        code = None
        if pd.notna(raw_code):
            try:
                # Handle 20840.0 -> "020840"
                code = str(int(float(raw_code))).zfill(6)
            except:
                pass
        
        # If not found or invalid, try map
        if not code:
            code = NAME_TO_CODE_MAP.get(p_name)
        
        if not code:
            # Fallback map search
            for key, val in NAME_TO_CODE_MAP.items():
                 clean_key = key.replace('\n', '').strip()
                 if clean_key in p_name: 
                    code = val
                    break
        
        if not code:
            if index < 20: 
                 print(f"Skipping unknown fund: '{p_name}'")
            continue

        # 4. Map Fields
        # Date: '确认日期'
        date_raw = row.get('确认日期')
        if pd.isna(date_raw):
            date_raw = row.get('交易时间') 
        
        # Clean newlines in date string if present
        date_str = normalize_date(str(date_raw).replace('\n', '').replace(' ', ''))
        # Fix date format if it comes out weird like "2026/02/0900:00" -> "2026-02-09"
        # The debug output showed "2026/02/0\n9 00:00" -> replace \n -> "2026/02/09 00:00"
        try:
             # Reparse with pandas to be safe
             date_str = pd.to_datetime(str(date_raw).replace('\n', '')).strftime('%Y-%m-%d')
        except:
             pass

        tx_type_raw = str(row.get('交易类型', ''))
        tx_type = map_trans_type(tx_type_raw)
        if tx_type == "UNKNOWN":
            continue
            
        # Amount: 确认金额
        amount = row.get('确认金额', 0.0)
        # Shares: 确认份额
        shares = row.get('确认份额', 0.0)
        # Fee: 手续费
        fee = row.get('手续费', 0.0)
        # NAV isn't explicitly in the columns shown in debug! 
        # But we can calculate it: Amount / Shares (if shares > 0)
        # Or look for '成交净值' which was missing in debug columns?
        # Debug cols: ['... '确认份额', 'Unnamed: 19', '手续费', ...]
        # '成交净值' is NOT in the columns list I saw.
        # But `inspect_excel` saw it earlier? Maybe hidden in Unnamed?
        # Let's verify '成交净值' calculation.
        
        nav = 0.0
        try:
            amount = float(amount) if not pd.isna(amount) and str(amount) != '/' else 0.0
            shares = float(shares) if not pd.isna(shares) and str(shares) != '/' else 0.0
            fee = float(fee) if not pd.isna(fee) and str(fee) != '/' else 0.0
            
            if shares > 0:
                nav = amount / shares
                # Round to 4 decimals
                nav = round(nav, 4)
        except:
            continue

        # Construct CSV Row
        valid_rows.append({
            "date": date_str,
            "trade_time": date_str,
            "code": code,
            "name": p_name,
            "type": tx_type,
            "amount": f"{amount:.2f}",
            "shares": f"{shares:.2f}",
            "nav": f"{nav:.4f}",
            "fee": f"{fee:.2f}",
            "remark": "Excel Import" 
        })
        
    # Write to CSV
    if valid_rows:
        out_df = pd.DataFrame(valid_rows).sort_values(by='date')
        records = out_df.to_dict('records')
        for row in records:
            row['external_id'] = f"{row['date']}:{row['code']}:{row['type']}:{row['amount']}:{row['shares']}:{row['nav']}"
        summary = upsert_source_transactions(
            db_path=get_db_path(os.path.dirname(OUTPUT_FILE) or '.'),
            csv_path=OUTPUT_FILE,
            transactions=records,
            source_type='excel_import',
            source_scope='default',
            dedupe_bootstrap=True,
        )
        print(f"Successfully synced {len(valid_rows)} Excel transactions.")
        print(f"Inserted rows: {summary['inserted']}")
        print(f"Skipped existing rows: {summary['skipped']}")
        print(f"Bootstrap rows replaced: {summary['bootstrap_replaced']}")
        print(f"CSV export: {OUTPUT_FILE}")
    else:
        print("No valid transactions found.")

if __name__ == "__main__":
    main()
