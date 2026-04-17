import pandas as pd
import urllib.request
import json

try:
    df = pd.read_csv('fund_data/transactions.csv', dtype={'code': str})
    df['code'] = df['code'].apply(lambda x: str(x).zfill(6))
    
    # Calculate shares: BUY adds, SELL subtracts
    df['signed_shares'] = df.apply(lambda row: float(row['shares']) if row['type'] == 'BUY' else -float(row['shares']), axis=1)
    
    holdings = df.groupby(['code', 'name'])['signed_shares'].sum().reset_index()
    holdings = holdings[holdings['signed_shares'] > 1e-4]
    
    print("=== Current Holdings ===")
    codes = []
    for _, row in holdings.iterrows():
        print(f"{row['code']} - {row['name']}: {row['signed_shares']:.2f} shares")
        codes.append(row['code'])
        
    print("\n=== Fetching Estimates ===")
    # try Sina API
    url = "https://hq.sinajs.cn/list=" + ",".join([f"f_{c}" for c in codes])
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
    with urllib.request.urlopen(req) as response:
        html = response.read().decode('gbk')
        for line in html.split('\n'):
            if line.strip():
                print(line)

except Exception as e:
    print(f"Error: {e}")
