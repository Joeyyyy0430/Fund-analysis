import streamlit as st
import pandas as pd
import requests
import re
import altair as alt
from datetime import datetime, timedelta
import html
import os
import tempfile
import time

from import_pdf_to_csv import load_pdf_snapshot
from trade_ledger import (
    enrich_transactions_with_fee_logic,
    fetch_transactions,
    get_db_path,
    replace_source_transactions,
    upsert_source_transactions,
)

# --- 页面配置 ---
st.set_page_config(page_title="FundOS", layout="wide", initial_sidebar_state="expanded")

# ===========================
# 1. 资产数据 & 持久化
# ===========================
DATA_DIR = "fund_data"
TX_FILE = os.path.join(DATA_DIR, "transactions.csv")
DB_FILE = get_db_path(DATA_DIR)

# 基金元数据 (Refined mapping for display/grouping)
# 修改后的 FUND_META 片段
FUND_META = {
    "008586": {"name": "华夏人工智能", "category": "AI & DATA", "bench": "AI"},
    "014130": {"name": "融通云计算", "category": "AI & DATA", "bench": "AI"},
    "020840": {"name": "南方半导体", "category": "AI & DATA", "bench": "SEMI"},
    "018463": {"name": "德邦稳盈增长", "category": "AI & DATA", "bench": "AI"},
    "sz002995": {"name": "天地在线", "category": "AI & DATA", "bench": "STOCK"},
    "004253": {"name": "国泰黄金 ETF", "category": "GOLD & PRECIOUS", "bench": "GOLD"}, # 补全此条
    "GOLD_CNY": {"name": "我的黄金", "category": "GOLD & PRECIOUS", "bench": "GOLD"},
    "sz002155": {"name": "湖南黄金", "category": "GOLD & PRECIOUS", "bench": "STOCK"},
    "015596": {"name": "国泰有色金属", "category": "RESOURCES", "bench": "RESOURCE"},
    "020274": {"name": "富国细分化工", "category": "RESOURCES", "bench": "RESOURCE"},
    "025733": {"name": "华安航天航空", "category": "MILITARY", "bench": "MILITARY"},
    "024195": {"name": "永赢商业卫星", "category": "MILITARY", "bench": "MILITARY"},
    "023639": {"name": "国泰电网设备", "category": "ENERGY", "bench": "NEW_ENERGY"},
    "019316": {"name": "易方达新能源", "category": "ENERGY", "bench": "NEW_ENERGY"},
    "021034": {"name": "易方达储能电池", "category": "ENERGY", "bench": "NEW_ENERGY"},
    "013511": {"name": "汇丰晋信低碳", "category": "ENERGY", "bench": "NEW_ENERGY"},
    "015686": {"name": "富国新兴产业", "category": "GROWTH", "bench": "MARKET"},
    "006479": {"name": "广发纳指100", "category": "GLOBAL", "bench": "NONE"},
}

CATEGORY_LABELS = {
    "AI & DATA": "AI 与算力",
    "GOLD & PRECIOUS": "黄金贵金属",
    "RESOURCES": "资源周期",
    "MILITARY": "军工航天",
    "ENERGY": "新能源",
    "GROWTH": "成长主题",
    "GLOBAL": "全球市场",
    "ACTIVE EQUITY": "主动权益",
    "HEALTH & CONSUMER": "医药消费",
    "FINANCE": "金融地产",
    "BROAD MARKET": "宽基指数",
    "CASH & BOND": "债券货币",
    "UNCATEGORIZED": "未分类",
    "OTHER": "其他",
}

INFERRED_META_RULES = [
    (("黄金", "贵金属"), "GOLD & PRECIOUS", "GOLD"),
    (("纳斯达克", "标普", "QDII", "全球", "海外", "恒生", "港股", "港股通"), "GLOBAL", "MARKET"),
    (("半导体", "芯片", "集成电路"), "AI & DATA", "SEMI"),
    (("人工智能", "云计算", "大数据", "算力", "软件", "通信"), "AI & DATA", "AI"),
    (("有色", "资源", "煤炭", "钢铁", "化工", "油气", "稀土", "金属"), "RESOURCES", "RESOURCE"),
    (("军工", "航天", "航空", "卫星", "国防"), "MILITARY", "MILITARY"),
    (("新能源", "光伏", "储能", "电池", "电网", "低碳", "环保"), "ENERGY", "NEW_ENERGY"),
    (("医药", "医疗", "生物", "创新药", "健康"), "HEALTH & CONSUMER", "HEALTH"),
    (("消费", "白酒", "食品", "农业", "家电"), "HEALTH & CONSUMER", "CONSUMER"),
    (("银行", "证券", "金融", "地产", "房地产", "保险"), "FINANCE", "FINANCE"),
    (("债", "货币", "现金", "短融", "同业存单"), "CASH & BOND", "NONE"),
    (("沪深300", "中证500", "中证1000", "创业板", "科创", "上证", "红利"), "BROAD MARKET", "MARKET"),
    (("混合", "股票", "成长", "价值", "优势", "趋势", "精选", "产业", "行业"), "ACTIVE EQUITY", "MARKET"),
]

TERM_TIPS = {
    "持仓市值": "按当前盘中估算净值或最新可得净值计算出来的持仓总价值。",
    "持仓本金": "当前仍留在仓位里的成本，不包含已经卖出的部分。",
    "累计盈亏": "当前持仓市值减去当前持仓本金后的结果。",
    "当日估算": "基于盘中估值推算出来的当日盈亏变动，不代表最终收盘确认结果。",
    "交易净影响": "选定交易日里，已实现盈亏和未平仓浮盈亏合并后的总结果。",
    "已实现盈亏": "卖出成交后已经锁定的盈亏，按持仓成本核算。",
    "未平仓浮盈": "当日买入后截至当前估值对应的浮动盈亏，还没有真正落袋。",
    "交易笔数": "该区块统计口径下被纳入计算的买卖成交笔数。",
    "官方估算": "基金官方或源站返回的盘中估值涨跌幅；若源站无数据，则显示不可用。",
    "穿透估算": "根据基金披露持仓和成分涨跌做出的估算结果，用来交叉验证官方估值。",
    "单位净值": "基金每一份额对应的净资产价值，通常按交易日披露。",
    "盘中估算净值": "根据最新净值和盘中估值涨跌幅推算的实时参考净值。",
    "成本价": "当前仍持有仓位的平均成本净值，等于持仓本金除以持有份额。",
    "持仓分布": "按当前仍持有的基金分类汇总，帮助你看清仓位集中在哪些主题。",
    "归因拆解": "把基金估值拆到持仓成分和剩余仓位，观察哪些部分在驱动当日表现。",
}

SPECIAL_POSITION_RULES = {
    # Gold-linked redemptions often stay visible in the broker holding page for an
    # extra settlement day, so keep them in displayed holdings slightly longer.
    "004253": {"sell_settlement_lag_days": 1},
}

SPECIAL_VALUATION_RULES = {
    # QDII linked funds track the A-share estimate more closely than the raw C-share
    # fundgz feed in the broker app.
    "006479": {"prefer_shadow_code_estimate": True},
    # Pending gold redemptions stay displayed closer to the previous confirmed NAV.
    "004253": {"prefer_previous_nav_when_pending_sell": True},
}

def load_transactions():
    """Load transactions from the SQLite ledger."""
    try:
        rows = fetch_transactions(DB_FILE, csv_path=TX_FILE)
        return enrich_transactions_with_fee_logic(rows)
    except Exception as e:
        st.error(f"Error loading transactions: {e}")
        return []


def normalize_fund_name(name, code=""):
    text = re.sub(r"\s+", " ", str(name or "").replace("\n", " ")).strip()
    invalid_names = {"", "nan", "none", "null", "unknown"}
    if text.lower() in invalid_names or text.startswith("Unknown("):
        return f"未知基金({code})" if code else "未知基金"
    return text


def build_fund_name_lookup(transactions):
    names = {}
    for tx in transactions:
        code = str(tx.get("code") or "").strip()
        if not code:
            continue
        name = normalize_fund_name(tx.get("name"), code)
        if not name.startswith("未知基金("):
            names[code] = name
    return names


def infer_fund_group(name):
    compact_name = re.sub(r"\s+", "", name or "")
    for keywords, category, bench in INFERRED_META_RULES:
        if any(keyword in compact_name for keyword in keywords):
            return category, bench
    return "UNCATEGORIZED", "MARKET"


def resolve_fund_meta(code, name_lookup):
    if code in FUND_META:
        return dict(FUND_META[code])

    display_name = normalize_fund_name(name_lookup.get(code), code)
    category, bench = infer_fund_group(display_name)
    return {"name": display_name, "category": category, "bench": bench}

def tx_sort_key(tx):
    return (
        tx.get('date', ''),
        tx.get('trade_time') or tx.get('date', ''),
        tx.get('external_id', ''),
    )


def parse_tx_date(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except Exception:
        return None


def get_display_share_adjustment(transactions, code, reference_date=None):
    if reference_date is None:
        reference_date = datetime.today().strftime('%Y-%m-%d')
    reference_day = parse_tx_date(reference_date)
    if reference_day is None:
        return 0.0

    lag_days = SPECIAL_POSITION_RULES.get(code, {}).get('sell_settlement_lag_days', 0)
    if not lag_days:
        return 0.0

    adjustment = 0.0
    for tx in transactions:
        if tx.get('code') != code or tx.get('type') != 'SELL':
            continue
        tx_day = parse_tx_date(tx.get('date'))
        if tx_day is None:
            continue
        day_delta = (reference_day - tx_day).days
        if 1 <= day_delta <= lag_days:
            try:
                adjustment += float(tx.get('shares') or 0)
            except Exception:
                continue
    return adjustment


def get_portfolio_from_transactions(
    transactions,
    include_same_day_buys=True,
    include_same_day_sells=False,
    reference_date=None,
):
    """Calculate holdings using the selected broker-style inclusion rules."""
    holdings = {}
    if reference_date is None:
        reference_date = datetime.today().strftime('%Y-%m-%d')
    reference_day = parse_tx_date(reference_date)

    for tx in sorted(transactions, key=tx_sort_key):
        tx_date = tx.get('date')
        if tx_date and tx_date > reference_date:
            continue
        if tx_date == reference_date:
            if tx['type'] == 'BUY' and not include_same_day_buys:
                continue
            if tx['type'] == 'SELL' and not include_same_day_sells:
                continue

        code = tx['code']
        if code not in holdings:
            holdings[code] = {'shares': 0.0, 'total_cost': 0.0, 'realized': 0.0}

        try:
            shares = float(tx['shares'])
            amount = float(tx['amount'])
            fee = float(tx.get('effective_fee', tx.get('fee') or 0))
        except Exception:
            continue

        if tx['type'] == 'BUY':
            holdings[code]['shares'] += shares
            holdings[code]['total_cost'] += amount + fee
        elif holdings[code]['shares'] > 0:
            avg_cost = holdings[code]['total_cost'] / holdings[code]['shares']
            cost_part = shares * avg_cost
            pnl = amount - fee - cost_part

            holdings[code]['shares'] -= shares
            holdings[code]['total_cost'] -= cost_part
            holdings[code]['realized'] += pnl

    return holdings


def calculate_today_trade_impact(transactions, valuation_map, analysis_date=None):
    """Estimate P/L impact attributable to trades executed on the analysis date."""
    available_dates = sorted({tx.get('date') for tx in transactions if tx.get('date')})
    if analysis_date is None:
        today_str = datetime.today().strftime('%Y-%m-%d')
        analysis_date = today_str if today_str in available_dates else (available_dates[-1] if available_dates else today_str)

    positions = {}
    detail_rows = []
    realized_total, floating_total = 0.0, 0.0

    for tx in sorted(transactions, key=tx_sort_key):
        code = tx['code']
        if code not in positions:
            positions[code] = {'shares': 0.0, 'cost': 0.0}

        try:
            shares = float(tx['shares'])
            amount = float(tx['amount'])
            fee = float(tx.get('effective_fee', tx.get('fee') or 0))
        except:
            continue

        tx_type = tx['type']
        is_target_day = tx.get('date') == analysis_date

        if is_target_day and tx_type == 'SELL':
            avg_cost = positions[code]['cost'] / positions[code]['shares'] if positions[code]['shares'] > 0 else 0.0
            cost_part = shares * avg_cost
            realized = amount - fee - cost_part
            realized_total += realized
            detail_rows.append({
                "TIME": (tx.get('trade_time') or tx.get('date', ''))[-8:],
                "CODE": code,
                "NAME": tx['name'],
                "TYPE": "SELL",
                "AMOUNT": amount,
                "SHARES": shares,
                "NAV": float(tx.get('nav') or 0),
                "REALIZED": realized,
                "FLOATING": 0.0,
                "IMPACT": realized,
            })

        if tx_type == 'BUY':
            positions[code]['shares'] += shares
            positions[code]['cost'] += amount + fee
        elif tx_type == 'SELL':
            if positions[code]['shares'] > 0:
                avg_cost = positions[code]['cost'] / positions[code]['shares']
                cost_part = shares * avg_cost
                positions[code]['shares'] -= shares
                positions[code]['cost'] -= cost_part

        if is_target_day and tx_type == 'BUY':
            valuation = valuation_map.get(code, {})
            current_nav = valuation.get('current_nav') or valuation.get('last_nav') or float(tx.get('nav') or 0)
            floating = shares * current_nav - amount - fee
            floating_total += floating
            detail_rows.append({
                "TIME": (tx.get('trade_time') or tx.get('date', ''))[-8:],
                "CODE": code,
                "NAME": tx['name'],
                "TYPE": "BUY",
                "AMOUNT": amount,
                "SHARES": shares,
                "NAV": float(tx.get('nav') or 0),
                "REALIZED": 0.0,
                "FLOATING": floating,
                "IMPACT": floating,
            })

    return {
        "date": analysis_date,
        "realized": realized_total,
        "floating": floating_total,
        "net": realized_total + floating_total,
        "details": detail_rows,
    }


def get_trade_dates(transactions, descending=False):
    dates = sorted({tx.get('date') for tx in transactions if tx.get('date')})
    return list(reversed(dates)) if descending else dates


def get_category_label(category):
    return CATEGORY_LABELS.get(category, category)


def pnl_color_class(value):
    return "text-red" if value >= 0 else "text-green"


def style_pnl(v):
    try:
        fv = float(v)
        if fv > 0:
            return 'color: #ff3b30'
        if fv < 0:
            return 'color: #34c759'
        return 'color: #d1d1d1'
    except:
        return 'color: #d1d1d1'


def format_metric_value(value, prefix="¥ ", decimals=2):
    if isinstance(value, (int, float)):
        return f"{prefix}{value:,.{decimals}f}"
    return f"{prefix}{value}"


def term_tip(label, tip_key=None):
    escaped_label = html.escape(label)
    return escaped_label


def render_page_header(kicker, title, description, meta_items=None):
    meta_html = ""
    if meta_items:
        pills = "".join(
            f"<span class=\"meta-pill\">{html.escape(str(item))}</span>" for item in meta_items if item
        )
        if pills:
            meta_html = f"<div class=\"meta-row\">{pills}</div>"

    desc_html = f"<div class='page-copy'>{html.escape(description)}</div>" if description else ""
    header_html = (
        "<div class=\"page-header\">"
        f"<div class=\"page-kicker\">{html.escape(kicker)}</div>"
        f"<div class=\"page-title\">{html.escape(title)}</div>"
        f"{desc_html}"
        f"{meta_html}"
        "</div>"
    )
    st.markdown(
        header_html,
        unsafe_allow_html=True
    )


def render_section_header(kicker, title, description=None, title_is_html=False):
    title_content = title if title_is_html else html.escape(title)
    desc_html = f"<div class='section-copy'>{html.escape(description)}</div>" if description else ""
    st.markdown(
        f"""
        <div class="section-head">
            <div class="section-kicker">{html.escape(kicker)}</div>
            <div class="section-title">{title_content}</div>
            {desc_html}
        </div>
        """,
        unsafe_allow_html=True
    )


def render_metric_card(
    col,
    label,
    value,
    sub,
    color_class="text-white",
    prefix="¥ ",
    decimals=2,
    label_is_html=False,
    sub_is_html=False,
):
    label_content = label if label_is_html else html.escape(label)
    sub_content = ""
    if sub:
        sub_rendered = sub if sub_is_html else html.escape(sub)
        sub_content = f"<div class='card-sub'>{sub_rendered}</div>"
    col.markdown(
        f"""
        <div class="data-card">
            <div class="card-label">{label_content}</div>
            <div class="card-value {color_class}">{format_metric_value(value, prefix=prefix, decimals=decimals)}</div>
            {sub_content}
        </div>
        """,
        unsafe_allow_html=True
    )


def render_note_panel(title, items, accent=False):
    rows = []
    for item in items:
        rows.append(
            f"""
            <div class="note-row">
                <div class="note-label">{html.escape(str(item.get('label', '')))}</div>
                <div class="note-value {item.get('value_class', '')}">{item.get('value', '')}</div>
            </div>
            """
        )

    st.markdown(
        f"""
        <div class="note-panel{' note-panel-accent' if accent else ''}">
            <div class="note-title">{html.escape(title)}</div>
            {''.join(rows)}
        </div>
        """,
        unsafe_allow_html=True
    )


def render_empty_state(title, description):
    st.markdown(
        f"""
        <div class="empty-state">
            <div class="empty-title">{html.escape(title)}</div>
            <div class="empty-copy">{html.escape(description)}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


def sync_uploaded_pdf(uploaded_file, snapshot=False):
    suffix = os.path.splitext(uploaded_file.name or "")[-1] or ".pdf"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(uploaded_file.getbuffer())
            temp_path = temp_file.name

        metadata, transactions = load_pdf_snapshot(temp_path)
        if not transactions:
            raise RuntimeError("未在 PDF 中识别到有效交易记录。")

        source_scope = metadata.get("source_scope") or "default"
        if snapshot:
            summary = replace_source_transactions(
                db_path=DB_FILE,
                csv_path=TX_FILE,
                transactions=transactions,
                source_type="ant_pdf",
                source_scope=source_scope,
                drop_bootstrap=True,
            )
        else:
            summary = upsert_source_transactions(
                db_path=DB_FILE,
                csv_path=TX_FILE,
                transactions=transactions,
                source_type="ant_pdf",
                source_scope=source_scope,
                dedupe_bootstrap=True,
            )

        trade_dates = sorted({row["date"] for row in transactions})
        return {
            "file_name": uploaded_file.name,
            "source_scope": source_scope,
            "trade_count": len(transactions),
            "date_range": (trade_dates[0], trade_dates[-1]) if trade_dates else None,
            "summary": summary,
            "snapshot": snapshot,
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

# Initial Load
TRANS_HISTORY = load_transactions()
HOLDINGS_STATE = get_portfolio_from_transactions(TRANS_HISTORY)
PREVIOUS_CLOSE_STATE = get_portfolio_from_transactions(
    TRANS_HISTORY,
    include_same_day_buys=False,
    include_same_day_sells=False,
)
FUND_NAME_LOOKUP = build_fund_name_lookup(TRANS_HISTORY)

# Construct Display Portfolio
MY_PORTFOLIO = {}
for code, data in HOLDINGS_STATE.items():
    if data['shares'] <= 0.001: continue # Skip empty positions (or handled purely as history?)
    
    meta = resolve_fund_meta(code, FUND_NAME_LOOKUP)
    cat = meta['category']
    if cat not in MY_PORTFOLIO: MY_PORTFOLIO[cat] = []
    
    # Estimate current value (Placeholder - will be updated in Main Loop or requires fetch here)
    # We will pass raw cost/shares and let the view calculate real-time value
    # But existing structure needs: (Name, Code, Bench, MktVal, Profit, Beta)
    # We will use 0.0 for MktVal/Profit initially, and update them.
    
    MY_PORTFOLIO[cat].append([
        f"{meta['name']} ({code})", 
        code, 
        meta['bench'], 
        0.0, # MktVal - To be filled
        0.0, # Profit - To be filled (Floating + Realized?) usually just floating in this view
        1.0, # Beta
        data['shares'], # [NEW] Extra field: Shares
        data['total_cost'] # [NEW] Extra field: Cost
    ])

# Check if portfolio is empty (first run)
if not MY_PORTFOLIO and not TRANS_HISTORY:
    # Default fallback or empty state
    pass

BENCHMARK_MAP = {
    "AI": ("515030", "人工智能ETF"),
    "SEMI": ("512480", "半导体ETF"),
    "RESOURCE": ("512400", "有色60ETF"),
    "MILITARY": ("512660", "军工ETF"),
    "NEW_ENERGY": ("516160", "新能源ETF"),
    "MARKET": ("510300", "沪深300ETF"),
    "HEALTH": ("512010", "医药ETF"),
    "CONSUMER": ("510150", "消费ETF"),
    "FINANCE": ("512800", "银行ETF"),
    "GOLD": ("518880", "黄金ETF"), 
    "STOCK": ("", "个股直连"),     
    "NONE": ("", "暂无数据")
}

# ===========================
# 2. 核心函数
# ===========================

# [新增] 影子代码映射表：解决C类份额无数据问题
REAL_CODE_MAP = {
    "004253": "000218",  # 国泰黄金 C -> A
    "015596": "160221",  # 国泰有色 C -> A
    "014130": "161628",  # 融通云计算 C -> A
    "025733": "025732",  # 华安航天 C -> A
    "006479": "270042",  # 广发纳指 C -> A
    "008586": "008585",  # 华夏人工智能 C -> A
    "020840": "020839",  # 南方半导体 C -> A
    "018463": "004260",  # 德邦稳盈 C -> A
    "015686": "001048",  # 富国新兴产业 C -> A
    "020274": "020273",  # 富国化工 C -> A
    "024195": "024194",  # 永赢卫星 C -> A
    "019316": "019315",  # 易方达新能源 C -> A
    "013511": "540008",  # 汇丰晋信低碳 C -> A
    "023639": "023638",  # 国泰电网 C -> A
    "GOLD_CNY": "sh518880" # 黄金 -> 黄金ETF
}

def get_real_code(code):
    """获取用于查询数据的真实代码"""
    return REAL_CODE_MAP.get(code, code)


def get_valuation_codes(code):
    primary = str(code or "").strip()
    mapped = get_real_code(primary)
    if mapped and mapped != primary:
        return [primary, mapped]
    return [primary]

def get_realtime_quote_sina(symbol):
    if not (symbol.startswith('sh') or symbol.startswith('sz') or symbol.startswith('bj')):
        if symbol.startswith('6') or symbol.startswith('5'): symbol = f"sh{symbol}"
        else: symbol = f"sz{symbol}"
    url = f"http://hq.sinajs.cn/list={symbol}"
    headers = {'Referer': 'https://finance.sina.com.cn'}
    try:
        response = requests.get(url, headers=headers, timeout=1)
        if '="' in response.text:
            data = response.text.split('="')[1].strip('";')
            fields = data.split(',')
            if len(fields) > 3:
                current = float(fields[3])
                prev = float(fields[2])
                time_str = fields[31] if len(fields) > 31 else ""
                rate = ((current - prev) / prev * 100) if prev > 0 else 0.0
                return {"rate": rate, "price": current, "time": time_str}
    except:
        pass
    return None

def get_official_valuation_universal(code):
    valuation_codes = get_valuation_codes(code)
    search_code = valuation_codes[0]

    # 1. 股票/ETF
    if search_code.startswith('sz') or search_code.startswith('sh'):
        q = get_realtime_quote_sina(search_code)
        if q:
            return {
                "est_rate": q['rate'],
                "time": q['time'],
                "type": "ETF/STOCK",
                "price": float(q.get('price') or 0),
                "dwjz": float(q.get('price') or 0),
                "gsz": float(q.get('price') or 0),
            }
        return None

    # 2. 基金接口，优先使用原始份额代码，只有拿不到时才回退到映射代码。
    for candidate in valuation_codes:
        url = f"http://fundgz.1234567.com.cn/js/{candidate}.js"
        try:
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                text = response.text.strip()
                if text.startswith("jsonpgz(") and text.endswith(");"):
                    json_str = text[len("jsonpgz("):-2].strip()
                    if json_str:
                        import json
                        data = json.loads(json_str)
                        time_raw = data.get('gztime', '')
                        time_clean = time_raw.split(' ')[-1] if ' ' in time_raw else time_raw
                        return {
                            "est_rate": float(data['gszzl']),
                            "time": time_clean,
                            "type": "FUND",
                            "code": candidate,
                            "jzrq": data.get('jzrq'),
                            "dwjz": float(data['dwjz']) if data.get('dwjz') else None,
                            "gsz": float(data['gsz']) if data.get('gsz') else None,
                        }
        except:
            pass
    
    # 3. 兜底
    q = get_realtime_quote_sina(search_code)
    if q:
        return {
            "est_rate": q['rate'],
            "time": q['time'],
            "type": "ETF/LOF",
            "price": float(q.get('price') or 0),
            "dwjz": float(q.get('price') or 0),
            "gsz": float(q.get('price') or 0),
        }
    
    return {"est_rate": None, "time": "NO_FEED", "type": "UNAVAILABLE"}


def get_display_nav(code, category, valuation_data, last_nav, today_str):
    special_rule = SPECIAL_VALUATION_RULES.get(code, {})
    if special_rule.get("prefer_shadow_code_estimate"):
        shadow_code = get_real_code(code)
        if shadow_code and shadow_code != code:
            shadow_data = get_official_valuation_universal(shadow_code)
            if shadow_data:
                if shadow_data.get('gsz') is not None:
                    return float(shadow_data['gsz'])
                if shadow_data.get('dwjz') is not None:
                    return float(shadow_data['dwjz'])

    if valuation_data:
        if valuation_data.get('type') in {'ETF/STOCK', 'ETF/LOF'} and valuation_data.get('price'):
            return float(valuation_data['price'])

        if category == "GLOBAL" and valuation_data.get('gsz') is not None and valuation_data.get('jzrq') != today_str:
            return float(valuation_data['gsz'])

        if valuation_data.get('dwjz') is not None:
            return float(valuation_data['dwjz'])

    return float(last_nav or 0)

@st.cache_data(ttl=3600)
def get_fund_holdings(fund_code):
    if fund_code.startswith('sz') or fund_code.startswith('sh') or fund_code == "GOLD_CNY": return None
    try:
        url = f"http://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={fund_code}&topline=10"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8'
        dfs = pd.read_html(response.text)
        if not dfs: return None
        df = dfs[0] 
        col_map = {}
        for col in df.columns:
            if '代码' in col: col_map['code'] = col
            elif '名称' in col: col_map['name'] = col
            elif '比例' in col or '占比' in col: col_map['weight'] = col
        if len(col_map) < 3: return None
        df = df[[col_map['code'], col_map['name'], col_map['weight']]].copy()
        df.columns = ['code', 'name', 'weight']
        df['code'] = df['code'].astype(str).str.zfill(6)
        df['weight'] = df['weight'].astype(str).str.replace('%', '', regex=False)
        df['weight'] = pd.to_numeric(df['weight'], errors='coerce').fillna(0.0)
        def format_symbol(code):
            code = str(code).strip()
            if code.startswith('6'): return f"sh{code}"
            if code.startswith('0') or code.startswith('3'): return f"sz{code}"
            if code.startswith('4') or code.startswith('8'): return f"bj{code}"
            return f"sz{code}"
        df['symbol'] = df['code'].apply(format_symbol)
        return df
    except:
        return None

def get_batch_quotes(symbol_list):
    symbol_list = [s for s in list(set(symbol_list)) if s]
    if not symbol_list: return {}
    quotes = {}
    chunk_size = 50
    for i in range(0, len(symbol_list), chunk_size):
        chunk = symbol_list[i:i+chunk_size]
        query_str = ",".join(chunk)
        url = f"http://hq.sinajs.cn/list={query_str}"
        headers = {'Referer': 'https://finance.sina.com.cn'}
        try:
            response = requests.get(url, headers=headers)
            lines = response.text.splitlines()
            for line in lines:
                if '="' in line:
                    var_name, data = line.split('="')
                    symbol = var_name.split('_')[-1]
                    data = data.strip('";')
                    fields = data.split(',')
                    if len(fields) > 3:
                        curr = float(fields[3])
                        prev = float(fields[2])
                        change = ((curr - prev) / prev) * 100 if prev > 0 else 0
                        quotes[symbol] = change
        except:
            pass
    return quotes

@st.cache_data(ttl=3600, show_spinner=False)  # Cache for 1 hour
def get_history_data(code, days_limit=365):
    """
    Fetch historical data for a fund or stock.
    Uses pagination for funds since EastMoney API returns 20 records per page.
    Cached for 1 hour since fund NAV updates once per day.
    """
    # 1. Use original code for history to ensure NAV alignment with transactions
    # Only map special abstract keys (like GOLD_CNY)
    if code == "GOLD_CNY":
        target_code = get_real_code(code)
    else:
        target_code = code
    
    is_stock = target_code.startswith('sz') or target_code.startswith('sh')
    df = pd.DataFrame()
    
    try:
        if is_stock:
            market = 1 if target_code.startswith('sh') else 0
            clean_code = target_code[2:]
            url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": f"{market}.{clean_code}",
                "klt": "101", "fqt": "1", "lmt": int(days_limit), "end": "20990101",
                "fields1": "f1", "fields2": "f51,f53"
            }
            res = requests.get(url, params=params, timeout=3)
            data = res.json()
            if data and 'data' in data and 'klines' in data['data']:
                klines = data['data']['klines']
                rows = []
                for k in klines:
                    d, p = k.split(',')
                    rows.append({"date": d, "value": float(p)})
                df = pd.DataFrame(rows)
        else:
            # Fund data - use pagination since API returns 20 records per page
            base_url = "http://api.fund.eastmoney.com/f10/lsjz"
            headers = {'Referer': 'http://fundf10.eastmoney.com/'}
            records_per_page = 20
            
            # Helper to fetch a single page
            def fetch_page(p_idx):
                try:
                    p = {"fundCode": target_code, "pageIndex": p_idx, "pageSize": records_per_page}
                    r = requests.get(base_url, params=p, headers=headers, timeout=5) # increased timeout
                    return p_idx, r.json()
                except:
                    return p_idx, None

            # 1. Fetch Page 1 to determine total count and get initial data
            _, data_p1 = fetch_page(1)
            
            all_records = []
            if data_p1 and 'Data' in data_p1 and 'LSJZList' in data_p1['Data']:
                lsjz = data_p1['Data']['LSJZList']
                if lsjz:
                    all_records.extend(lsjz)
                    
                    total_count = data_p1.get('TotalCount', 0)
                    if not total_count and 'Data' in data_p1 and isinstance(data_p1['Data'], dict):
                         total_count = data_p1['Data'].get('TotalCount', 0)
                    
                    # Calculate pages needed
                    needed_records = int(days_limit)
                    if len(all_records) < needed_records:
                        total_pages = (total_count + records_per_page - 1) // records_per_page
                        needed_pages = (needed_records + records_per_page - 1) // records_per_page
                        # Cap at total available pages
                        pages_to_fetch = min(total_pages, needed_pages)
                        
                        if pages_to_fetch > 1:
                            # Parallel execution for remaining pages
                            # Use ThreadPoolExecutor
                            from concurrent.futures import ThreadPoolExecutor, as_completed
                            
                            # Max workers can be tuned. 10 is usually safe for light APIs.
                            results_map = {}
                            with ThreadPoolExecutor(max_workers=10) as executor:
                                futures = [executor.submit(fetch_page, p) for p in range(2, pages_to_fetch + 1)]
                                for future in as_completed(futures):
                                    pid, res = future.result()
                                    if res and 'Data' in res and 'LSJZList' in res['Data']:
                                        results_map[pid] = res['Data']['LSJZList']
                            
                            # Assemble in order
                            for p in range(2, pages_to_fetch + 1):
                                if p in results_map:
                                    all_records.extend(results_map[p])

            # Process collected records
            if all_records:
                # API returns newest first, keep that order and trim to requested amount
                rows = []
                for item in all_records[:int(days_limit)]:  # Take first N records (newest)
                    if item.get('DWJZ'):
                        rows.append({"date": item['FSRQ'], "value": float(item['DWJZ'])})
                df = pd.DataFrame(rows)
                # Reverse to get chronological order (oldest first) for charting
                if not df.empty:
                    df = df.iloc[::-1].reset_index(drop=True)

        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            
    except Exception as e:
        print(f"Err history: {e}")
    
    return df

def get_nav_at_date(code, date_str):
    """Wait and fetch NAV for a specific date (Sync/Block if needed, but here we just query history)."""
    # Try to find in history first
    df = get_history_data(code, 365) # Cache hit usually
    if not df.empty:
        # Convert date_str to datetime
        target_date = pd.to_datetime(date_str)
        # Find exact match or closest previous? Fund should be exact or next day T+1 confirmation.
        # Usually user enters Trade Date. Logic:
        # Fund: Trade Date T (before 15:00) -> Nav of T.
        # We assume date_str IS the date of NAV generation.
        row = df[df['date'] == target_date]
        if not row.empty:
            return float(row.iloc[0]['value'])
    
            
    # 2. Try Local Transactions (Fallback for "Future"/Simulation Data)
    # Check loaded global TRANS_HISTORY
    for tx in TRANS_HISTORY:
        if tx['code'] == code and tx['date'] == date_str:
            try:
                return float(tx['nav'])
            except:
                pass
                
    return None

# --- 赛博图表渲染 (增强版: 交易点 + 百分比显示) ---

def calculate_indicators(df):
    """Calculate RSI and MACD for the dataframe."""
    df = df.copy()
    # RSI (14)
    delta = df['value'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # MACD (12, 26, 9)
    exp1 = df['value'].ewm(span=12, adjust=False).mean()
    exp2 = df['value'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    return df

# --- 赛博图表渲染 (增强版: 交易点 + 百分比显示) ---
def render_cyber_chart(df, transactions=None, display_days=None, intraday_est_rate=None, average_cost=None):
    if transactions is None:
        transactions = []

    if df.empty:
        st.info("暂无走势图数据")
        return

    # 1. Calc Indicators (on full data)
    df = calculate_indicators(df)
    
    try:
        df['date'] = pd.to_datetime(df['date'])
    except:
        pass
    
    df = df.sort_values('date') 
    
    # 2. Slice for Display
    if display_days:
        # Keep last N days
        df = df.iloc[-int(display_days):]
    
    if df.empty:
        st.info("当前时间范围内暂无数据")
        return
    
    # Calculate percentage change relative to fund inception (NAV = ~1.0000 or Base)
    # User requested: 0% is inception (1.0).
    base_value = 1.0000
    
    df['pct_change'] = ((df['value'] - base_value) / base_value) * 100
    df['change'] = df['value'].pct_change().fillna(0) * 100  # Daily change for tooltip
    
    # Display current NAV info
    current_nav = df.iloc[-1]['value']
    estimated_nav = current_nav * (1 + intraday_est_rate / 100) if intraday_est_rate is not None else None

    # Calculate total return since inception (or relative to 1.0)
    total_return = ((current_nav - base_value) / base_value) * 100
    pct_color = '#34c759' if total_return >= 0 else '#ff3b30'

    avg_cost_value = None
    cost_delta = None
    if average_cost is not None:
        try:
            avg_cost_value = float(average_cost)
        except:
            avg_cost_value = None
    if avg_cost_value is not None and avg_cost_value > 0:
        cost_delta = ((current_nav / avg_cost_value) - 1) * 100

    st.markdown(f"""
        <div style='display:flex; flex-wrap:wrap; gap:14px; align-items:center; font-size:12px; color:#666; margin:4px 0 6px;'>
            <span>{term_tip('单位净值')}: <span style='color:#00f2ea; font-weight:bold;'>{current_nav:.4f}</span></span>
            <span style='color:{pct_color}; font-weight:bold;'>{total_return:+.2f}%</span>
            <span style='color:#666;'>(成立以来)</span>
            <span>{term_tip('盘中估算净值')}: <span style='color:#00f2ea; font-weight:bold;'>{f'{estimated_nav:.4f}' if estimated_nav is not None else '暂无'}</span></span>
            <span>{term_tip('成本价')}: <span style='color:#f5f5f5; font-weight:bold;'>{f'{avg_cost_value:.4f}' if avg_cost_value is not None and avg_cost_value > 0 else '暂无'}</span></span>
            <span style='color:{'#ff3b30' if cost_delta is not None and cost_delta >= 0 else '#34c759' if cost_delta is not None else '#666'}; font-weight:bold;'>{f'{cost_delta:+.2f}% vs 成本' if cost_delta is not None else ''}</span>
        </div>
    """, unsafe_allow_html=True)
    
    # 交互选择器 (Shared)
    hover = alt.selection_point(
        fields=['date'],
        nearest=True,
        on='mouseover',
        empty=False,
    )

    # --- MAIN CHART ---
    base = alt.Chart(df).encode(
        x=alt.X('date:T', axis=alt.Axis(format='%m-%d', title=None, grid=False, domain=False, tickColor='#333', labelColor='#666'))
    )

    # Area & Line
    main_line = base.mark_line(color='#00f2ea', strokeWidth=2).encode(
        y=alt.Y('pct_change:Q', scale=alt.Scale(zero=False, padding=5), axis=alt.Axis(title='涨跌 (%)', grid=True, gridColor='#1a1a1a', gridDash=[2,2], labelColor='#666', format='+.1f'))
    )
    main_area = base.mark_area(color='#00f2ea', opacity=0.3, line=False).encode(
        y='pct_change:Q'
    )
    
    main_layers = [main_area, main_line]

    # Zero Line
    zero_line = alt.Chart(pd.DataFrame({'y': [0]})).mark_rule(color='#666', strokeDash=[5, 5], strokeWidth=1).encode(y='y:Q')
    main_layers.append(zero_line)

    if avg_cost_value is not None and avg_cost_value > 0:
        cost_chart_y = ((avg_cost_value - base_value) / base_value) * 100
        cost_df = pd.DataFrame([{"y": cost_chart_y, "label": f"成本价 {avg_cost_value:.4f}"}])
        cost_rule = alt.Chart(cost_df).mark_rule(color='#f5f5f5', strokeDash=[6, 4], strokeWidth=1.2, opacity=0.9).encode(y='y:Q')
        cost_label = alt.Chart(cost_df).mark_text(
            align='left',
            dx=6,
            dy=-6,
            color='#f5f5f5',
            fontSize=10
        ).encode(y='y:Q', text='label')
        main_layers.append(cost_rule)
        main_layers.append(cost_label)

    # Grid Trading Lines (Last Buy based)
    last_buy_price = 0.0
    if transactions:
        # Find last buy
        sorted_tx = sorted(transactions, key=lambda x: x['date'])
        for tx in sorted_tx:
            if tx['type'] == 'BUY':
                 try: last_buy_price = float(tx['nav'])
                 except: pass
                 if last_buy_price == 0: # Try fetch from history
                     pass 

    if last_buy_price > 0:
        # Calculate target levels: Sell at +5%, +10%
        levels = [0.05, 0.10] 
        grid_data = []
        for l in levels:
            target_p = last_buy_price * (1 + l)
            chart_y = ((target_p - base_value) / base_value) * 100
            grid_data.append({"y": chart_y, "label": f"止盈 +{int(l*100)}%"})
            
        grid_df = pd.DataFrame(grid_data)
        grid_rules = alt.Chart(grid_df).mark_rule(color='#ff3b30', strokeDash=[2,2], opacity=0.6).encode(y='y:Q')
        grid_labels = alt.Chart(grid_df).mark_text(align='left', dx=5, color='#ff3b30', fontSize=10).encode(y='y:Q', text='label')
        main_layers.append(grid_rules)
        main_layers.append(grid_labels)


    # Transaction Markers
    if transactions:
        tx_data = []
        chart_start = pd.to_datetime(df['date'].min())
        chart_end = pd.to_datetime(df['date'].max())
        
        for tx in transactions:
             t_date = pd.to_datetime(tx['date'])
             
             # Filter based on chart date range
             if t_date < chart_start or t_date > chart_end:
                 continue

             # Use the transaction's recorded NAV preferably, or fallback to current history
             val = 0.0
             try:
                 val = float(tx['nav'])
             except:
                 pass
             
             if val == 0:
                 # Fallback: find in history
                 matches = df[df['date'] == t_date]
                 if not matches.empty:
                     val = float(matches.iloc[0]['value'])
             
             # [FIX] Always align marker to the CHART's line (Historical Data)
             # accessible via 'df'
             chart_val = 0.0
             matches = df[df['date'] == t_date]
             if not matches.empty:
                 chart_val = float(matches.iloc[0]['value'])
             
             # If we can't find it in chart (e.g. weekend), we might fallback to transaction val
             # But for alignment, chart_val is preferred.
             display_y = chart_val if chart_val > 0 else val

             if display_y > 0:
                 # Calculate pct_change relative to the CHART'S base_value
                 tx_pct = ((display_y - base_value) / base_value) * 100
                 
                 tx_data.append({
                     "date": t_date,  # Keep as Timestamp
                     "value": float(val), # Keep original transaction price for Tooltip
                     "pct_change": tx_pct,
                     "type": str(tx['type']),
                     "amt": str(f"¥{float(tx['amount']):.0f}") if tx['type'] == 'BUY' else str(f"{float(tx['shares']):.0f}份")
                 })
        
        if tx_data:
            tx_df = pd.DataFrame(tx_data)
            tx_df['date'] = pd.to_datetime(tx_df['date']) # Ensure datetime type
            
            # Use explicit charts for buy and sell to ensure data is correctly registered in Vega-Lite
            
            # Buy Points (Red Triangle Up)
            buy_df = tx_df[tx_df['type'] == 'BUY']
            if not buy_df.empty:
                buys = alt.Chart(buy_df).mark_point(
                    shape='triangle-up', 
                    size=200, 
                    color='#ff3b30', 
                    filled=True,
                    opacity=1,
                    clip=False
                ).encode(
                    x=alt.X('date:T'),
                    y='pct_change:Q',
                    tooltip=[
                        alt.Tooltip('date:T', format='%Y-%m-%d', title='日期'), 
                        alt.Tooltip('value:Q', title='成交价', format='.4f'),
                        alt.Tooltip('amt', title='金额'),
                        alt.Tooltip('pct_change:Q', title='涨跌', format='+.2f')
                    ]
                )
                main_layers.append(buys)
                
            # Sell Points (Green Triangle Down)
            sell_df = tx_df[tx_df['type'] == 'SELL']
            if not sell_df.empty:
                sells = alt.Chart(sell_df).mark_point(
                    shape='triangle-down', 
                    size=200, 
                    color='#34c759', 
                    filled=True,
                    opacity=1,
                    clip=False
                ).encode(
                    x=alt.X('date:T'),
                    y='pct_change:Q',
                    tooltip=[
                        alt.Tooltip('date:T', format='%Y-%m-%d', title='日期'), 
                        alt.Tooltip('value:Q', title='成交价', format='.4f'),
                        alt.Tooltip('amt', title='份额'),
                        alt.Tooltip('pct_change:Q', title='涨跌', format='+.2f')
                    ]
                )
                main_layers.append(sells)

    # 组合 - Move params here to ensure global scope for interaction
    chart = alt.layer(*main_layers).properties(
        height=250,
    ).add_params(
        hover
    )


    # --- RSI CHART ---
    rsi_base = alt.Chart(df).encode(x=alt.X('date:T', axis=None)) # Hide X axis
    rsi_line = rsi_base.mark_line(color='#ff9f0a', strokeWidth=1.5).encode(
        y=alt.Y('rsi:Q', scale=alt.Scale(domain=[0, 100]), axis=alt.Axis(title='RSI', tickCount=3, gridColor='#1a1a1a'))
    )
    # Grid lines for RSI: 30, 40, 50, 60, 70
    rsi_grids = []
    # Grid lines for RSI: 30, 40, 50, 60, 70
    rsi_grids = []
    for val in [30, 40, 50, 60, 70]:
        # Brighter colors and higher opacity for dark mode
        col = '#ff3b30' if val >= 70 else '#34c759' if val <= 30 else '#888'
        op = 0.8 if val in [30, 70] else 0.5
        rule = alt.Chart(pd.DataFrame({'y': [val]})).mark_rule(color=col, strokeDash=[4,4] if val in [30,70] else [2,2], opacity=op).encode(y='y:Q')
        rsi_grids.append(rule)
    
    rsi_chart = alt.layer(rsi_line, *rsi_grids).properties(height=80)

    # --- MACD CHART ---
    macd_base = alt.Chart(df).encode(x=alt.X('date:T', axis=None))
    macd_bar = macd_base.mark_bar().encode(
        y=alt.Y('macd_hist:Q', axis=alt.Axis(title='MACD', tickCount=3, gridColor='#1a1a1a')),
        color=alt.condition(alt.datum.macd_hist > 0, alt.value('#ff3b30'), alt.value('#34c759'))
    )
    macd_line = macd_base.mark_line(color='#00f2ea', strokeWidth=1).encode(y='macd:Q')
    signal_line = macd_base.mark_line(color='#ff9f0a', strokeWidth=1).encode(y='macd_signal:Q')
    
    macd_chart = alt.layer(macd_bar, macd_line, signal_line).properties(height=80)

    # Combine
    final_chart = alt.vconcat(chart, rsi_chart, macd_chart).resolve_scale(x='shared').configure_view(strokeWidth=0).configure_axis(grid=False)
    st.altair_chart(final_chart, use_container_width=True, theme=None)



# ===========================
# 3. CSS 样式
# ===========================
st.markdown("""
<style>
    :root {
        --bg-0: #050505;
        --bg-1: #080808;
        --bg-2: #0b0b0c;
        --bg-3: #101114;
        --line-1: #17181c;
        --line-2: #23262d;
        --text-1: #f2f3f5;
        --text-2: #a2a8b3;
        --text-3: #5f6773;
        --accent: #00f2ea;
        --accent-soft: rgba(0, 242, 234, 0.08);
        --radius-lg: 20px;
        --radius-md: 14px;
    }
    [data-testid="stActionButtonIcon"], [data-testid="stElementToolbar"], button[title="View fullscreen"] { display: none !important; }
    .stApp {
        background:
            radial-gradient(circle at top right, rgba(0, 242, 234, 0.06), transparent 32%),
            linear-gradient(180deg, #060607 0%, #050505 100%);
        color: #d1d1d1;
        font-family: "SF Pro Text", sans-serif;
    }
    [data-testid="stSidebar"] {
        background:
            linear-gradient(180deg, rgba(0, 242, 234, 0.02) 0%, transparent 18%),
            #080808 !important;
        border-right: 1px solid var(--line-1);
    }
    [data-testid="block-container"] {
        max-width: 1380px;
        padding-top: 2.2rem;
        padding-bottom: 3rem;
    }
    [data-testid="stSidebarUserContent"] {
        padding-top: 2.4rem;
    }
    .sidebar-title {
        font-family: 'SF Mono', monospace;
        font-size: 15px;
        color: var(--accent);
        letter-spacing: 5px;
        padding: 12px 0 0 0;
        text-align: left;
        margin: 0;
        line-height: 1;
    }
    .sidebar-divider {
        height: 1px;
        margin: 18px 0 18px;
        background: linear-gradient(90deg, rgba(0, 242, 234, 0.2), rgba(255,255,255,0.04) 55%, transparent);
    }
    .sidebar-section-label {
        font-family: 'SF Mono', monospace;
        font-size: 10px;
        color: var(--text-3);
        letter-spacing: 1.8px;
        margin: 0 0 8px 4px;
        text-transform: uppercase;
    }
    
    /* 按钮重置 */
    .stButton button {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: #666 !important;
        transition: all 0.2s !important;
        white-space: nowrap !important;
    }
    .stButton button:hover { color: #fff !important; }
    .stButton button:focus { box-shadow: none !important; }
    
    /* Sidebar tool buttons */
    div[data-testid="stSidebarUserContent"] button[kind="primary"] {
        min-height: 40px !important;
        border: 1px solid rgba(0, 242, 234, 0.24) !important;
        color: var(--accent) !important;
        font-family: 'SF Mono', monospace;
        font-size: 12px !important;
        font-weight: 600 !important;
        letter-spacing: 0.6px !important;
        margin-top: 0 !important;
        padding: 0 12px !important;
        border-radius: 12px !important;
        background: linear-gradient(180deg, rgba(0, 242, 234, 0.08), rgba(0, 242, 234, 0.025)) !important;
    }
    div[data-testid="stSidebarUserContent"] button[kind="primary"]:hover {
        border-color: rgba(0, 242, 234, 0.46) !important;
        background-color: var(--accent-soft) !important;
    }
    div[data-testid="stSidebarUserContent"] button[kind="primary"]:disabled {
        border-color: var(--line-1) !important;
        color: var(--text-3) !important;
        background: rgba(255,255,255,0.015) !important;
    }
    
    /* 卡片样式 */
    .page-header {
        background:
            linear-gradient(180deg, rgba(0, 242, 234, 0.05) 0%, rgba(0, 242, 234, 0.0) 100%),
            var(--bg-2);
        border: 1px solid var(--line-1);
        border-radius: 24px;
        padding: 28px 30px;
        margin-bottom: 26px;
        position: relative;
        overflow: hidden;
    }
    .page-header::after {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(90deg, rgba(255,255,255,0.02), transparent 24%);
        pointer-events: none;
    }
    .page-kicker, .section-kicker {
        font-family: 'SF Mono', monospace;
        font-size: 11px;
        letter-spacing: 2.8px;
        text-transform: uppercase;
        color: var(--accent);
        margin-bottom: 12px;
    }
    .page-title {
        color: var(--text-1);
        font-size: clamp(28px, 3.2vw, 34px);
        font-weight: 300;
        line-height: 1.05;
        margin-bottom: 10px;
        letter-spacing: 0.4px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .term-tip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        cursor: help;
    }
    .tip-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 16px;
        height: 16px;
        border-radius: 999px;
        border: 1px solid rgba(0, 242, 234, 0.35);
        color: var(--accent);
        font-size: 10px;
        line-height: 1;
    }
    .page-copy {
        color: var(--text-2);
        font-size: 14px;
        line-height: 1.6;
        max-width: 760px;
    }
    .meta-row {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 18px;
    }
    .meta-pill {
        font-family: 'SF Mono', monospace;
        font-size: 11px;
        letter-spacing: 1px;
        color: #cbd2dc;
        padding: 8px 12px;
        border: 1px solid var(--line-2);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.02);
    }
    .section-head {
        margin: 30px 0 16px 0;
    }
    .section-title {
        color: var(--text-1);
        font-size: 19px;
        font-weight: 500;
        margin-bottom: 6px;
        letter-spacing: 0.2px;
    }
    .section-copy {
        color: var(--text-2);
        font-size: 13px;
        line-height: 1.55;
        max-width: 760px;
    }
    .data-card {
        background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent 100%), #0b0b0c;
        border: 1px solid var(--line-1);
        border-radius: var(--radius-lg);
        padding: 24px;
        min-height: 146px;
        height: 100%;
        display: flex;
        flex-direction: column;
        justify-content: center;
        position: relative;
        overflow: hidden;
    }
    .data-card::before {
        content: "";
        position: absolute;
        top: 0;
        left: 24px;
        right: 24px;
        height: 1px;
        background: linear-gradient(90deg, rgba(0,242,234,0.35), transparent);
    }
    .card-label {
        font-family: 'SF Mono', monospace;
        font-size: 10px;
        color: var(--text-3);
        margin-bottom: 14px;
        letter-spacing: 2px;
    }
    .card-value {
        font-family: 'SF Mono', monospace;
        font-size: 30px;
        font-weight: 300;
        line-height: 1.05;
        color: var(--text-1);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .card-sub {
        font-family: 'SF Mono', monospace;
        font-size: 12px;
        color: var(--text-2);
        margin-top: 12px;
        min-height: 18px;
        line-height: 1.45;
    }
    .note-panel {
        background: linear-gradient(180deg, rgba(255,255,255,0.015), transparent 100%), var(--bg-2);
        border: 1px solid var(--line-1);
        border-radius: var(--radius-lg);
        padding: 20px 22px;
        height: 100%;
    }
    .note-panel-accent {
        box-shadow: inset 0 0 0 1px rgba(0, 242, 234, 0.12);
    }
    .note-title {
        font-family: 'SF Mono', monospace;
        font-size: 10px;
        color: var(--accent);
        letter-spacing: 2px;
        margin-bottom: 16px;
    }
    .note-row {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        padding: 12px 0;
        border-top: 1px solid rgba(255,255,255,0.04);
    }
    .note-row:first-of-type {
        border-top: none;
        padding-top: 0;
    }
    .note-label {
        color: var(--text-2);
        font-size: 12px;
        line-height: 1.45;
    }
    .note-value {
        color: var(--text-1);
        font-family: 'SF Mono', monospace;
        font-size: 12px;
        text-align: right;
        line-height: 1.45;
    }
    .empty-state {
        background: linear-gradient(180deg, rgba(0, 242, 234, 0.04), transparent 100%), var(--bg-2);
        border: 1px solid var(--line-1);
        border-radius: var(--radius-lg);
        padding: 24px 26px;
        margin-top: 14px;
    }
    .empty-title {
        color: var(--text-1);
        font-size: 16px;
        font-weight: 500;
        margin-bottom: 6px;
    }
    .empty-copy {
        color: var(--text-2);
        font-size: 13px;
        line-height: 1.55;
        max-width: 520px;
    }
    .field-note {
        color: var(--text-2);
        font-size: 12px;
        line-height: 1.5;
        margin-top: 4px;
    }
    .compact-gap {
        margin-top: 8px;
    }
    .range-shell {
        background: rgba(255,255,255,0.02);
        border: 1px solid var(--line-1);
        border-radius: 18px;
        padding: 14px;
        margin: 18px 0 22px;
    }
    .range-shell .stButton button {
        border-radius: 999px !important;
        min-height: 36px !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] {
        width: 100%;
        margin-bottom: 18px;
        --primary-color: var(--accent);
        --primary-color-light: rgba(0, 242, 234, 0.12);
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] > div {
        width: 100%;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 6px;
        padding: 4px;
        border: 1px solid var(--line-1);
        border-radius: 16px;
        background: rgba(255,255,255,0.018);
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button {
        width: 100% !important;
        min-height: 40px !important;
        border-radius: 12px !important;
        padding: 0 10px !important;
        justify-content: center !important;
        color: var(--text-2) !important;
        font-size: 13px !important;
        font-weight: 600 !important;
        line-height: 1 !important;
        letter-spacing: 0 !important;
        white-space: nowrap !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button p {
        margin: 0 !important;
        white-space: nowrap !important;
        line-height: 1 !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button[aria-pressed="true"],
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button[kind="segmented_controlActive"] {
        color: var(--text-1) !important;
        border-color: rgba(0, 242, 234, 0.34) !important;
        background: linear-gradient(180deg, rgba(0, 242, 234, 0.16), rgba(0, 242, 234, 0.055)) !important;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.05) !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button[aria-pressed="true"] *,
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button[kind="segmented_controlActive"] * {
        color: var(--text-1) !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stSegmentedControl"] button:hover {
        color: var(--text-1) !important;
        border-color: rgba(0, 242, 234, 0.26) !important;
        background: rgba(255,255,255,0.035) !important;
    }
    .stSelectbox label, .stDateInput label, .stNumberInput label {
        font-family: 'SF Mono', monospace;
        font-size: 10px !important;
        letter-spacing: 1.8px;
        color: var(--text-3) !important;
    }
    .stFileUploader label, .stToggle label {
        font-family: 'SF Mono', monospace;
        font-size: 10px !important;
        letter-spacing: 1.8px;
        color: var(--text-3) !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stCheckbox"] {
        margin: 8px 0 2px;
        min-height: 30px;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stCheckbox"] label {
        min-height: 30px;
        padding: 0 !important;
        display: flex !important;
        align-items: center !important;
        gap: 10px !important;
        cursor: pointer;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stCheckbox"] label > div:first-child {
        margin: 0 !important;
        flex: 0 0 auto;
        align-self: center !important;
        transform: translateY(0) !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stCheckbox"] label > div:last-child {
        display: flex !important;
        align-items: center !important;
        min-height: 30px !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stCheckbox"] p {
        color: var(--text-2) !important;
        font-size: 12px !important;
        font-weight: 500 !important;
        line-height: 18px !important;
        margin: 0 !important;
        white-space: nowrap !important;
        display: inline-flex !important;
        align-items: center !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stCheckbox"] [data-testid="stTooltipHoverTarget"] {
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        vertical-align: middle !important;
        margin-left: 4px !important;
        height: 18px !important;
        line-height: 18px !important;
    }
    [data-testid="stSidebarUserContent"] .stToggle p,
    [data-testid="stSidebarUserContent"] .stFileUploader label p {
        white-space: nowrap;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stExpander"] details {
        background: linear-gradient(180deg, rgba(255,255,255,0.018), rgba(255,255,255,0.008));
        border: 1px solid var(--line-2);
        border-radius: 16px;
        overflow: visible;
        margin-top: 0;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }
    [data-testid="stSidebarUserContent"] [data-testid="stExpander"] summary {
        min-height: 44px;
        padding-top: 4px;
        padding-bottom: 4px;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stExpander"] summary:hover {
        background: rgba(255,255,255,0.018);
    }
    [data-testid="stSidebarUserContent"] [data-testid="stExpander"] summary p {
        font-size: 13px !important;
        font-weight: 600 !important;
        color: var(--text-1) !important;
        letter-spacing: 0 !important;
        white-space: nowrap !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stExpander"] details[open] summary {
        border-bottom: 1px solid rgba(255,255,255,0.05);
        margin-bottom: 6px;
    }
    [data-testid="stSidebarUserContent"] [data-testid="stExpander"] details > div {
        padding: 8px 16px 10px !important;
    }
    [data-testid="stFileUploaderDropzone"] {
        position: relative;
        min-height: auto;
        padding: 2px 0 30px 0 !important;
        display: flex;
        align-items: center;
        justify-content: center;
        background: transparent !important;
        border: none !important;
        margin-bottom: 10px;
    }
    [data-testid="stFileUploaderDropzone"] section,
    [data-testid="stFileUploaderDropzone"] small,
    [data-testid="stFileUploaderDropzone"] > div > div > *:not(button) {
        display: none !important;
    }
    [data-testid="stFileUploaderDropzone"]::after {
        content: "支持单个 PDF，最大 200MB";
        position: absolute;
        left: 50%;
        transform: translateX(-50%);
        bottom: 4px;
        color: var(--text-2);
        font-size: 12px;
        line-height: 1.2;
        pointer-events: none;
        z-index: 1;
        white-space: nowrap;
    }
    [data-testid="stFileUploaderDropzone"] > div {
        width: 100%;
        display: flex;
        justify-content: center;
        align-items: center;
    }
    [data-testid="stFileUploaderDropzone"] button {
        font-size: 0 !important;
        position: relative;
        margin: 0 auto !important;
        min-height: 42px !important;
        width: auto !important;
        min-width: 168px !important;
        max-width: 100% !important;
        padding: 0 24px !important;
        border-radius: 14px !important;
        border: 1px solid rgba(0, 242, 234, 0.24) !important;
        background: linear-gradient(180deg, rgba(0, 242, 234, 0.12), rgba(0, 242, 234, 0.05)) !important;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 0 0 1px rgba(0, 242, 234, 0.04) !important;
        color: var(--text-1) !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    [data-testid="stFileUploaderDropzone"] button:hover {
        border-color: rgba(0, 242, 234, 0.55) !important;
        background: linear-gradient(180deg, rgba(0, 242, 234, 0.16), rgba(0, 242, 234, 0.06)) !important;
    }
    [data-testid="stFileUploaderDropzone"] button * {
        font-size: 0 !important;
    }
    [data-testid="stFileUploaderDropzone"] button::before {
        content: "选择 PDF 文件";
        font-size: 13px;
        color: var(--text-1);
        font-weight: 600;
        letter-spacing: 0;
    }
    [data-testid="stSidebarUserContent"] .nav-pill-row {
        margin: 0 0 14px;
    }
    [data-testid="stSidebarUserContent"] .nav-shell {
        margin-bottom: 18px;
    }
    [data-baseweb="select"] > div,
    [data-baseweb="input"] > div,
    [data-testid="stDateInput"] > div > div {
        background: var(--bg-2) !important;
        border-color: var(--line-2) !important;
        border-radius: 14px !important;
    }
    [data-baseweb="select"] span, [data-baseweb="input"] input {
        color: var(--text-1) !important;
    }
    [data-testid="stDataFrame"] {
        border: 1px solid var(--line-1);
        border-radius: 18px;
        overflow: hidden;
        background: var(--bg-2);
    }
    [data-testid="stDataFrame"] [role="grid"] {
        border-radius: 18px;
    }
    [data-testid="stAlert"] {
        border-radius: 16px;
        border: 1px solid var(--line-1);
        background: var(--bg-2);
    }

    .text-red { color: #ff3b30 !important; }
    .text-green { color: #34c759 !important; }
    .text-white { color: var(--text-1) !important; }
</style>
""", unsafe_allow_html=True)

# ===========================
# 4. 侧边栏逻辑
# ===========================
with st.sidebar:
    st.markdown('<div class="sidebar-title">FUND.OS</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-divider"></div>', unsafe_allow_html=True)
    if 'nav_mode' not in st.session_state: st.session_state.nav_mode = "OVERVIEW"
    trade_dates_desc = get_trade_dates(TRANS_HISTORY, descending=True)
    if 'review_date' not in st.session_state:
        st.session_state.review_date = trade_dates_desc[0] if trade_dates_desc else datetime.today().strftime('%Y-%m-%d')
    if 'review_date_select' not in st.session_state:
        st.session_state.review_date_select = st.session_state.review_date
    if 'upload_sync_feedback' not in st.session_state:
        st.session_state.upload_sync_feedback = None

    st.markdown('<div class="sidebar-section-label">导航</div>', unsafe_allow_html=True)
    nav_options = ["OVERVIEW", "REVIEW", "DETAILS"]
    nav_labels = {"OVERVIEW": "总览", "REVIEW": "复盘", "DETAILS": "详情"}
    nav_value = st.segmented_control(
        "导航",
        nav_options,
        default=st.session_state.nav_mode,
        format_func=lambda value: nav_labels[value],
        key="nav_segmented_control",
        label_visibility="collapsed",
        width="stretch",
    )
    if nav_value and nav_value != st.session_state.nav_mode:
        st.session_state.nav_mode = nav_value
        st.rerun()

    st.markdown('<div class="sidebar-section-label">数据同步</div>', unsafe_allow_html=True)
    if st.button("刷新数据", key="sidebar_refresh", help="清除缓存并重新拉取行情、估值和持仓计算", icon=":material/refresh:", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    with st.expander("上传交易记录 PDF", expanded=False):
        uploaded_pdf = st.file_uploader(
            "PDF 文件",
            type=["pdf"],
            key="trade_pdf_uploader",
            help="支持最近一周、最近一月或完整交易明细 PDF。默认按增量去重导入。",
            label_visibility="collapsed",
        )
        snapshot_mode = st.checkbox(
            "覆盖旧 PDF 记录",
            value=False,
            help="关闭时按增量追加去重；开启时用这份 PDF 替换同账户旧的 PDF 记录。"
        )
        if uploaded_pdf and st.button("同步交易记录", key="sync_trades_btn", icon=":material/sync:", type="primary", width="stretch"):
            try:
                result = sync_uploaded_pdf(uploaded_pdf, snapshot=snapshot_mode)
                st.session_state.upload_sync_feedback = result
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"同步失败：{exc}")

        if st.session_state.upload_sync_feedback:
            feedback = st.session_state.upload_sync_feedback
            date_range = feedback.get("date_range")
            summary = feedback.get("summary", {})
            if feedback.get("snapshot"):
                msg = (
                    f"已同步 {feedback['trade_count']} 条记录，范围 {date_range[0]} 到 {date_range[1]}。"
                    f" 替换旧 PDF 记录 {summary.get('replaced', 0)} 条，移除 bootstrap {summary.get('dropped_bootstrap', 0)} 条。"
                )
            else:
                msg = (
                    f"已同步 {feedback['trade_count']} 条记录，范围 {date_range[0]} 到 {date_range[1]}。"
                    f" 新增 {summary.get('inserted', 0)} 条，跳过重复 {summary.get('skipped', 0)} 条，替换 bootstrap {summary.get('bootstrap_replaced', 0)} 条。"
                )
            st.success(msg)
    
    st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)

    all_funds = [f for cat, funds in MY_PORTFOLIO.items() for f in funds]
    if not all_funds:
        st.warning("暂无持仓数据，请先上传交易记录 PDF。")
    else:
        if 'current_code' not in st.session_state and all_funds:
            f = all_funds[0]
            st.session_state.current_code, st.session_state.current_name, st.session_state.current_bench = f[1], f[0], f[2]
            st.session_state.current_beta = f[5] if len(f) > 5 else 1.0

    if st.session_state.nav_mode == "DETAILS":
        for category, funds in MY_PORTFOLIO.items():
            st.markdown(f"<div style='font-size:9px; color:#333; padding:20px 15px 5px; letter-spacing:2px; font-family:SF Mono;'>//{get_category_label(category)}</div>", unsafe_allow_html=True)
            for fund_data in funds:
                f_name, f_code, f_bench = fund_data[:3]
                f_beta = fund_data[5] if len(fund_data) > 5 else 1.0
                simple_name = f_name.split('(')[0].strip()
                is_selected = st.session_state.current_code == f_code
                
                border_col = "#00f2ea" if is_selected else "transparent"
                text_col = "#00f2ea" if is_selected else "#666"
                st.markdown(f"""<style>div[data-testid="stSidebarUserContent"] .stButton button[key="{f_code}"] {{
                    border-left: 2px solid {border_col} !important; color: {text_col} !important;
                    text-align: left !important; padding-left: 15px !important; }}</style>""", unsafe_allow_html=True)

                if st.button(simple_name, key=f_code, type="secondary"):
                    st.session_state.current_code = f_code
                    st.session_state.current_name = f_name
                    st.session_state.current_bench = f_bench
                    st.session_state.current_beta = f_beta
                    st.session_state.chart_range = "近10天" # 切换基金时重置
                    st.rerun()

# ===========================
# 5. 主界面
# ===========================
if st.session_state.nav_mode == "OVERVIEW":
    latest_trade_dates = get_trade_dates(TRANS_HISTORY, descending=True)
    latest_trade_date = latest_trade_dates[0] if latest_trade_dates else "暂无交易"
    render_page_header(
        "总览",
        "组合总览",
        "",
        meta_items=[
            f"{len(all_funds)} 只持仓基金",
            f"{len(MY_PORTFOLIO)} 个策略分组",
            f"最近交易日 {latest_trade_date}",
        ]
    )

    total_mkt, total_prof, total_today = 0.0, 0.0, 0.0
    today_str = datetime.today().strftime('%Y-%m-%d')
    valuation_map = {}
    for f in all_funds:
        # f structure: [Name, Code, Bench, MktVal(0), Profit(0), Beta, Shares, Cost]
        code = f[1]
        shares = f[6]
        cost = f[7]
        meta = resolve_fund_meta(code, FUND_NAME_LOOKUP)
        display_shares = shares + get_display_share_adjustment(TRANS_HISTORY, code, today_str)
        
        d = get_official_valuation_universal(code)
        last_nav = 0.0
        prev_nav = 0.0
        hist = get_history_data(code, 5)
        if not hist.empty:
            last_nav = hist.iloc[-1]['value']
            prev_nav = hist.iloc[-2]['value'] if len(hist) >= 2 else last_nav
            
        est_rate = 0.0
        if d and d.get('est_rate') is not None:
            est_rate = d['est_rate']

        display_nav = get_display_nav(code, meta['category'], d, last_nav, today_str)
        if (
            SPECIAL_VALUATION_RULES.get(code, {}).get("prefer_previous_nav_when_pending_sell")
            and display_shares > shares
            and prev_nav > 0
        ):
            display_nav = prev_nav
        current_nav = d.get('gsz') if d and d.get('gsz') is not None else display_nav
        overnight_shares = PREVIOUS_CLOSE_STATE.get(code, {}).get('shares', 0.0)
        overnight_shares += get_display_share_adjustment(TRANS_HISTORY, code, today_str)
        yesterday_profit = overnight_shares * (last_nav - prev_nav) if last_nav and prev_nav else 0.0
        valuation_map[code] = {
            "last_nav": last_nav,
            "current_nav": current_nav,
            "display_nav": display_nav,
            "display_shares": display_shares,
            "est_rate": est_rate,
            "yesterday_profit": yesterday_profit,
        }
        
        mkt_val = display_shares * display_nav
        profit = mkt_val - cost
        
        # Update list for Display (Hack to update tuple/list in place)
        f[3] = mkt_val
        f[4] = profit
        
        total_mkt += mkt_val
        total_prof += profit
        total_today += yesterday_profit

    total_cost = total_mkt - total_prof
    summary_c1, summary_c2, summary_c3, summary_c4 = st.columns(4)
    render_metric_card(summary_c1, term_tip("持仓市值"), total_mkt, "", label_is_html=True)
    render_metric_card(summary_c2, term_tip("持仓本金"), total_cost, "", label_is_html=True)
    render_metric_card(
        summary_c3,
        term_tip("累计盈亏"),
        total_prof,
        f"收益率 {(total_prof / total_cost * 100 if total_cost else 0):+.2f}%",
        pnl_color_class(total_prof),
        label_is_html=True
    )
    render_metric_card(
        summary_c4,
        term_tip("当日估算"),
        total_today,
        f"今日变动 {(total_today / total_mkt * 100 if total_mkt else 0):+.2f}%",
        pnl_color_class(total_today),
        label_is_html=True
    )

    allocation_rows = []
    for category, funds in MY_PORTFOLIO.items():
        allocation_rows.append({
            "分类": get_category_label(category),
            "基金数": len(funds),
            "持仓市值": sum(f[3] for f in funds),
            "盈亏": sum(f[4] for f in funds),
        })

    render_section_header(
        "持仓",
        term_tip("持仓分布"),
        "",
        title_is_html=True
    )
    if allocation_rows:
        allocation_df = pd.DataFrame(allocation_rows).sort_values("持仓市值", ascending=False)
        st.dataframe(
            allocation_df.style.format(
                {
                    "持仓市值": "¥{:.2f}",
                    "盈亏": "¥{:+.2f}",
                }
            ).map(style_pnl, subset=["盈亏"]),
            use_container_width=True,
            hide_index=True
        )

        holding_rows = []
        for category, funds in MY_PORTFOLIO.items():
            for fund in funds:
                suffix = f" ({fund[1]})"
                display_name = fund[0][:-len(suffix)] if fund[0].endswith(suffix) else fund[0]
                holding_rows.append({
                    "基金": display_name,
                    "代码": fund[1],
                    "分类": get_category_label(category),
                    "份额": fund[6],
                    "持仓本金": fund[7],
                    "持仓市值": fund[3],
                    "盈亏": fund[4],
                })

        render_section_header(
            "明细",
            "持仓明细",
            ""
        )
        holding_df = pd.DataFrame(holding_rows).sort_values("持仓市值", ascending=False)
        st.dataframe(
            holding_df.style.format(
                {
                    "份额": "{:.2f}",
                    "持仓本金": "¥{:.2f}",
                    "持仓市值": "¥{:.2f}",
                    "盈亏": "¥{:+.2f}",
                }
            ).map(style_pnl, subset=["盈亏"]),
            use_container_width=True,
            hide_index=True
        )
    else:
        render_empty_state("暂无持仓", "先上传交易记录 PDF，页面才会按分类汇总你的当前仓位。")

    today_trade_impact = calculate_today_trade_impact(TRANS_HISTORY, valuation_map)
    if today_trade_impact["details"]:
        trade_df = pd.DataFrame(today_trade_impact["details"]).rename(
            columns={
                "TIME": "时间",
                "CODE": "代码",
                "NAME": "基金",
                "TYPE": "类型",
                "AMOUNT": "金额",
                "SHARES": "份额",
                "NAV": "单位净值",
                "REALIZED": "已实现盈亏",
                "FLOATING": "未平仓浮盈",
                "IMPACT": "交易净影响",
            }
        )
        trade_df["类型"] = trade_df["类型"].map({"BUY": "买入", "SELL": "卖出"}).fillna(trade_df["类型"])

        render_section_header(
            "交易",
            f"最近交易日影响 // {today_trade_impact['date']}",
            "",
        )
        t1, t2, t3, t4 = st.columns(4)
        render_metric_card(
            t1,
            term_tip("交易净影响"),
            today_trade_impact["net"],
            "已实现盈亏 + 未平仓浮盈",
            pnl_color_class(today_trade_impact["net"]),
            label_is_html=True
        )
        render_metric_card(
            t2,
            term_tip("已实现盈亏"),
            today_trade_impact["realized"],
            "仅统计卖出成交",
            pnl_color_class(today_trade_impact["realized"]),
            label_is_html=True
        )
        render_metric_card(
            t3,
            term_tip("未平仓浮盈"),
            today_trade_impact["floating"],
            "当日买入按当前估值计算",
            pnl_color_class(today_trade_impact["floating"]),
            label_is_html=True
        )
        render_metric_card(
            t4,
            term_tip("交易笔数"),
            len(trade_df),
            f"{trade_df['代码'].nunique()} 只基金发生交易",
            "text-white",
            prefix="",
            decimals=0,
            label_is_html=True
        )

        render_section_header(
            "明细",
            "交易明细",
            ""
        )
        st.dataframe(
            trade_df.style.format(
                {
                    "金额": "¥{:.2f}",
                    "份额": "{:.2f}",
                    "单位净值": "{:.4f}",
                    "已实现盈亏": "¥{:+.2f}",
                    "未平仓浮盈": "¥{:+.2f}",
                    "交易净影响": "¥{:+.2f}",
                }
            ).map(style_pnl, subset=["已实现盈亏", "未平仓浮盈", "交易净影响"]),
            use_container_width=True,
            hide_index=True
        )
    else:
        render_section_header(
            "交易",
            f"最近交易日影响 // {today_trade_impact['date']}",
            ""
        )
        render_empty_state(
            "这一天没有交易",
            "一旦有交易日存在，这里会自动展示已实现盈亏、未平仓浮盈和逐笔交易影响。"
        )

elif st.session_state.nav_mode == "REVIEW":
    trade_dates_desc = get_trade_dates(TRANS_HISTORY, descending=True)
    latest_review_date = trade_dates_desc[0] if trade_dates_desc else "暂无交易"
    render_page_header(
        "复盘",
        "交易日复盘",
        "",
        meta_items=[
            f"{len(trade_dates_desc)} 个交易日" if trade_dates_desc else "0 个交易日",
            f"最近可复盘日期 {latest_review_date}",
        ]
    )

    if not trade_dates_desc:
        render_empty_state("暂无交易历史", "先上传交易记录 PDF，页面才可以按日期回放你的每一个交易日。")
    else:
        if st.session_state.review_date not in trade_dates_desc:
            st.session_state.review_date = trade_dates_desc[0]
        if st.session_state.review_date_select not in trade_dates_desc:
            st.session_state.review_date_select = st.session_state.review_date
        current_review_date = st.session_state.review_date
        current_idx = trade_dates_desc.index(current_review_date)
        render_section_header(
            "控制",
            "复盘日期",
            ""
        )

        nav_c1, nav_c2, nav_c3 = st.columns([1.1, 3.8, 1.1])
        if nav_c1.button("较新", use_container_width=True, disabled=current_idx == 0):
            next_date = trade_dates_desc[current_idx - 1]
            st.session_state.review_date = next_date
            st.session_state.review_date_select = next_date
            st.rerun()

        if nav_c3.button("更早", use_container_width=True, disabled=current_idx == len(trade_dates_desc) - 1):
            next_date = trade_dates_desc[current_idx + 1]
            st.session_state.review_date = next_date
            st.session_state.review_date_select = next_date
            st.rerun()

        review_date = nav_c2.selectbox(
            "交易日期",
            trade_dates_desc,
            key="review_date_select"
        )
        if review_date != st.session_state.review_date:
            st.session_state.review_date = review_date
            st.rerun()

        current_idx = trade_dates_desc.index(review_date)

        review_codes = sorted({tx['code'] for tx in TRANS_HISTORY if tx.get('date') == review_date})

        review_valuation_map = {}
        for code in review_codes:
            off_data = get_official_valuation_universal(code)
            hist = get_history_data(code, 5)
            last_nav = float(hist.iloc[-1]['value']) if not hist.empty else 0.0
            est_rate = off_data['est_rate'] if off_data and off_data.get('est_rate') is not None else 0.0
            current_nav = last_nav * (1 + est_rate / 100) if last_nav > 0 else 0.0
            review_valuation_map[code] = {"last_nav": last_nav, "current_nav": current_nav, "est_rate": est_rate}

        trade_review = calculate_today_trade_impact(TRANS_HISTORY, review_valuation_map, analysis_date=review_date)
        detail_df = pd.DataFrame(trade_review["details"]).rename(
            columns={
                "TIME": "时间",
                "CODE": "代码",
                "NAME": "基金",
                "TYPE": "类型",
                "AMOUNT": "金额",
                "SHARES": "份额",
                "NAV": "单位净值",
                "REALIZED": "已实现盈亏",
                "FLOATING": "未平仓浮盈",
                "IMPACT": "交易净影响",
            }
        )
        detail_df["类型"] = detail_df["类型"].map({"BUY": "买入", "SELL": "卖出"}).fillna(detail_df["类型"])

        c1, c2, c3, c4 = st.columns(4)
        render_metric_card(
            c1,
            term_tip("交易净影响"),
            trade_review["net"],
            "已实现盈亏 + 未平仓浮盈",
            pnl_color_class(trade_review["net"]),
            label_is_html=True
        )
        render_metric_card(
            c2,
            term_tip("已实现盈亏"),
            trade_review["realized"],
            "仅统计卖出成交",
            pnl_color_class(trade_review["realized"]),
            label_is_html=True
        )
        render_metric_card(
            c3,
            term_tip("未平仓浮盈"),
            trade_review["floating"],
            "当日买入按当前估值计算",
            pnl_color_class(trade_review["floating"]),
            label_is_html=True
        )
        render_metric_card(
            c4,
            term_tip("交易笔数"),
            len(trade_review["details"]),
            f"{len(review_codes)} 只基金",
            "text-white",
            prefix="",
            decimals=0,
            label_is_html=True
        )

        if detail_df.empty:
            render_empty_state("这一天没有交易", "从上面的日期控件切到别的交易日，就可以看到实际发生过的买卖复盘。")
        else:
            summary_df = (
                detail_df.groupby(["代码", "基金"], as_index=False)
                .agg(
                    交易笔数=("类型", "count"),
                    买卖方向=("类型", lambda vals: "/".join(sorted(set(vals)))),
                    金额=("金额", "sum"),
                    已实现盈亏=("已实现盈亏", "sum"),
                    未平仓浮盈=("未平仓浮盈", "sum"),
                    交易净影响=("交易净影响", "sum"),
                )
                .sort_values("交易净影响", ascending=True)
            )

            ordered_df = detail_df.sort_values(["时间", "代码", "类型"])
            render_section_header(
                "汇总",
                f"基金级结果 // {review_date}",
                ""
            )
            st.dataframe(
                summary_df.style.format(
                    {
                        "金额": "¥{:.2f}",
                        "已实现盈亏": "¥{:+.2f}",
                        "未平仓浮盈": "¥{:+.2f}",
                        "交易净影响": "¥{:+.2f}",
                    }
                ).map(style_pnl, subset=["已实现盈亏", "未平仓浮盈", "交易净影响"]),
                use_container_width=True,
                hide_index=True
            )

            detail_df = ordered_df[["时间", "代码", "基金", "类型", "金额", "份额", "单位净值", "已实现盈亏", "未平仓浮盈", "交易净影响"]]

            render_section_header(
                "明细",
                f"逐笔执行 // {review_date}",
                ""
            )
            st.dataframe(
                detail_df.style.format(
                    {
                        "金额": "¥{:.2f}",
                        "份额": "{:.2f}",
                        "单位净值": "{:.4f}",
                        "已实现盈亏": "¥{:+.2f}",
                        "未平仓浮盈": "¥{:+.2f}",
                        "交易净影响": "¥{:+.2f}",
                    }
                ).map(style_pnl, subset=["已实现盈亏", "未平仓浮盈", "交易净影响"]),
                use_container_width=True,
                hide_index=True
            )

elif st.session_state.nav_mode == "DETAILS":
    target_code = st.session_state.current_code
    target_beta = st.session_state.get('current_beta', 1.0)
    bench_code, bench_name = BENCHMARK_MAP.get(st.session_state.current_bench, ("", ""))

    render_page_header(
        "详情",
        st.session_state.current_name.split('(')[0],
        "",
        meta_items=[
            f"代码 {target_code}",
            f"基准 {bench_name}",
            f"Beta {target_beta}",
        ]
    )

    off_data = get_official_valuation_universal(target_code)
    is_stock = target_code.startswith('sz') or target_code.startswith('sh') or target_code == 'GOLD_CNY'

    render_section_header(
        "信号",
        "盘中信号解读",
        ""
    )
    # --- 估值归因 ---
    df_holdings, my_est, show_bd = None, 0.0, False
    if is_stock:
        my_est = off_data['est_rate'] if off_data and off_data.get('est_rate') is not None else 0.0
    else:
        df_holdings = get_fund_holdings(target_code)
        if df_holdings is not None and not df_holdings.empty:
            syms = df_holdings['symbol'].tolist()
            if bench_code: syms.append(f"sh{bench_code}")
            qs = get_batch_quotes(syms)
            df_holdings['change'] = df_holdings['symbol'].map(qs).fillna(0.0)
            df_holdings['contrib'] = df_holdings['weight'] * df_holdings['change'] / 100
            w_top10 = df_holdings['weight'].sum()
            chg_bench = qs.get(f"sh{bench_code}", 0.0)
            contrib_other = (100 - w_top10) * chg_bench * target_beta / 100
            my_est = df_holdings['contrib'].sum() + contrib_other
            show_bd = True
    
    # --- 查找持仓 ---
    current_holding_val = 0.0
    avg_cost_nav = None
    for f_tuple in all_funds:
        if f_tuple[1] == target_code:
            current_holding_val = f_tuple[3] # This is now dynamically calculated MktVal
            try:
                holding_shares = float(f_tuple[6])
                holding_cost = float(f_tuple[7])
                if holding_shares > 0:
                    avg_cost_nav = holding_cost / holding_shares
            except:
                avg_cost_nav = None
            break

    c1, c2 = st.columns(2)
    
    # 官方卡片
    rate_off = off_data['est_rate'] if off_data else None
    has_official_feed = rate_off is not None
    col_off = "text-white" if not has_official_feed else ("text-red" if rate_off >= 0 else "text-green")
    sub_off = f"时间：{off_data['time'] if off_data else '暂无'}"
    if current_holding_val > 0 and has_official_feed:
        pl_off = current_holding_val * rate_off / 100
        sub_off = f"估算金额：<span style='color:{'#ff3b30' if pl_off>=0 else '#34c759'}'>¥ {pl_off:+.2f}</span>"
    elif off_data and off_data.get('type') == "UNAVAILABLE":
        sub_off = "官方盘中估算暂不可用"

    c1.markdown(f"""<div class="data-card"><div class="card-label">{term_tip('官方估算')}</div>
    <div class="card-value {col_off}">{f'{rate_off:+.2f}%' if has_official_feed else '暂无'}</div>
    <div class="card-sub">{sub_off}</div></div>""", unsafe_allow_html=True)
    
    # 穿透卡片
    col_my = "text-red" if my_est >= 0 else "text-green"
    sub_my = "状态：实时估算"
    if current_holding_val > 0:
        pl_my = current_holding_val * my_est / 100
        sub_my = f"估算金额：<span style='color:{'#ff3b30' if pl_my>=0 else '#34c759'}'>¥ {pl_my:+.2f}</span>"

    c2.markdown(f"""<div class="data-card"><div class="card-label">{term_tip('穿透估算')}</div>
    <div class="card-value {col_my}">{my_est:+.2f}%</div>
    <div class="card-sub">{sub_my}</div></div>""", unsafe_allow_html=True)

    # --- 走势图 ---
    range_map = {"近10天": 10, "近1月": 22, "近半年": 125, "近1年": 250, "成立来": 20000}
    if 'chart_range' not in st.session_state: st.session_state.chart_range = "近10天"

    render_section_header(
        "走势",
        "净值走势与信号",
        ""
    )
    cols = st.columns(len(range_map))

    for i, (label, days) in enumerate(range_map.items()):
        is_active = st.session_state.chart_range == label
        btn_key = f"range_{label}"

        border_col = "#00f2ea" if is_active else "#333"
        bg_col = "rgba(0, 242, 234, 0.1)" if is_active else "transparent"
        text_col = "#00f2ea" if is_active else "#666"

        st.markdown(f"""<style>div[data-testid="stVerticalBlock"] button[key="{btn_key}"] {{ 
            color: {text_col} !important; border: 1px solid {border_col} !important; 
            background-color: {bg_col} !important; }}</style>""", unsafe_allow_html=True)

        if cols[i].button(label, key=btn_key, use_container_width=True):
            st.session_state.chart_range = label
            st.rerun()

    target_days = range_map[st.session_state.chart_range]
    buffer_days = 60
    fetch_days = target_days + buffer_days
    df_hist_full = get_history_data(target_code, fetch_days)

    code_tx = [t for t in TRANS_HISTORY if t['code'] == target_code]
    render_cyber_chart(
        df_hist_full,
        code_tx,
        display_days=target_days,
        intraday_est_rate=off_data['est_rate'] if off_data and off_data.get('est_rate') is not None else None,
        average_cost=avg_cost_nav
    )
    st.markdown("<div class='field-note'>短周期更适合看节奏，长周期更适合看仓位是否仍处在你能接受的大趋势里。</div>", unsafe_allow_html=True)

    if show_bd and df_holdings is not None:
        render_section_header(
            "归因",
            term_tip("归因拆解"),
            "",
            title_is_html=True
        )
        dd = df_holdings[['code', 'name', 'weight', 'change', 'contrib']].copy()
        dd.columns = ['代码', '名称', '权重%', '涨跌%', '贡献%']
        dd.loc[len(dd)] = [bench_code, f"非重仓 [Beta:{target_beta}]", 100-w_top10, chg_bench, contrib_other]
        st.dataframe(
            dd.style.format("{:.2f}", subset=['权重%','涨跌%','贡献%']).map(style_pnl, subset=['涨跌%','贡献%']),
            use_container_width=True,
            hide_index=True
        )
    elif not is_stock:
        render_section_header(
            "归因",
            term_tip("归因拆解"),
            "",
            title_is_html=True
        )
        render_empty_state(
            "暂时无法归因",
            "等持仓源返回数据后，这里会展示重仓成分贡献和非重仓部分的估算结果。"
        )
