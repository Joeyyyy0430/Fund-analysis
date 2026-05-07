import argparse
import csv
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
from trade_ledger import get_db_path, replace_source_transactions, upsert_source_transactions

DEFAULT_OUTPUT_FILE = "fund_data/transactions.csv"

# Keep the app aligned with the tracked portfolio universe.
EXCLUDE_KEYWORDS = [
    "中欧医疗健康",
    "永赢先进制造",
]

CANONICAL_NAME_MAP = {
    "004253": "国泰黄金ETF联接C",
    "006479": "广发纳斯达克100ETF联接(QDII)C",
    "008586": "华夏人工智能ETF联接C",
    "013511": "汇丰晋信低碳先锋股票C",
    "014130": "融通中证云计算与大数据主题指数(LOF)C",
    "015596": "国泰国证有色金属行业指数C",
    "015686": "富国新兴产业股票C",
    "018463": "德邦稳盈增长灵活配置混合C",
    "019316": "易方达中证新能源ETF联接C",
    "020274": "富国中证细分化工产业主题ETF联接C",
    "020840": "南方中证半导体产业指数C",
    "021034": "易方达储能电池ETF联接C",
    "023639": "国泰A股电网设备ETF联接C",
    "024195": "永赢国证商用卫星通信产业ETF联接C",
    "025733": "华安国证航天航空行业ETF联接C",
}

BUY_TYPES = {"用户买入", "定投买入"}
SELL_TYPES = {"用户卖出"}

COLUMN_ALIASES = {
    "order_id": ["订单号", "交易单号", "流水号"],
    "tx_datetime": ["确认时间", "确认日期", "交易时间", "申请时间", "创建时间"],
    "raw_type": ["交易类型", "业务类型"],
    "name": ["基金名称", "产品名称", "资产名称"],
    "code": ["基金代码", "产品代码", "资产代码"],
    "apply_amount": ["申请金额", "申请金额(元)", "交易金额"],
    "apply_shares": ["申请份额", "申请份额(份)"],
    "confirm_amount": ["确认金额", "确认金额(元)", "成交金额"],
    "confirm_shares": ["确认份额", "确认份额(份)", "成交份额"],
    "fee": ["手续费", "交易费用", "费用"],
}

FALLBACK_COLUMNS = {
    "order_id": 0,
    "tx_datetime": 1,
    "raw_type": 2,
    "name": 3,
    "code": 5,
    "apply_amount": 6,
    "apply_shares": 7,
    "confirm_amount": 8,
    "confirm_shares": 9,
    "fee": 10,
}


def clean_text(value):
    return (value or "").replace("\n", "").replace(",", "").strip()


def parse_decimal(value):
    text = clean_text(value).replace("元", "").replace("份", "").replace("%", "")
    if not text or text == "/":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_datetime(value):
    raw_text = str(value or "").replace(",", "").strip()
    compact_text = clean_text(value)
    spaced_text = re.sub(r"\s+", " ", raw_text.replace("\n", " ")).strip()
    candidates = [text for text in (spaced_text, compact_text) if text]
    if not candidates:
        return None
    for text in candidates:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def backup_existing_file(path):
    if not os.path.exists(path):
        return None
    base, ext = os.path.splitext(path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{base}_backup_{timestamp}{ext}"
    shutil.copy2(path, backup_path)
    return backup_path


def normalize_name(code, raw_name):
    canonical = CANONICAL_NAME_MAP.get(code)
    return canonical or raw_name


def normalize_header(value):
    text = clean_text(value)
    text = text.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", text)


def detect_column_map(row, previous_map):
    headers = [normalize_header(cell) for cell in row]
    if not any("交易类型" in header or "业务类型" in header for header in headers):
        return previous_map

    column_map = {}
    for field, aliases in COLUMN_ALIASES.items():
        normalized_aliases = [normalize_header(alias) for alias in aliases]
        for alias in normalized_aliases:
            for index, header in enumerate(headers):
                if alias and alias in header:
                    column_map[field] = index
                    break
            if field in column_map:
                break
    return column_map or previous_map


def row_value(row, column_map, field):
    index = column_map.get(field, FALLBACK_COLUMNS.get(field))
    if index is None or index >= len(row):
        return ""
    return row[index]


def extract_code(row, column_map):
    raw_code = clean_text(row_value(row, column_map, "code"))
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", raw_code)
    if match:
        return match.group(1)

    for cell in row:
        text = clean_text(cell)
        if text.isdigit() and len(text) == 6:
            return text
    return ""


def extract_name(row, column_map, code):
    name = clean_text(row_value(row, column_map, "name"))
    code_index = column_map.get("code", FALLBACK_COLUMNS.get("code"))
    if (not name or name == code) and code_index and code_index < len(row):
        previous_index = code_index - 1
        if previous_index >= 0:
            name = clean_text(row[previous_index])
    return name


def extract_transaction_row(row, column_map, seq, page_number, row_index):
    raw_type = clean_text(row_value(row, column_map, "raw_type"))
    if not raw_type or raw_type == "交易类型":
        return None

    code = extract_code(row, column_map)
    if not (code.isdigit() and len(code) == 6):
        return None

    name = extract_name(row, column_map, code)
    if any(keyword in name for keyword in EXCLUDE_KEYWORDS):
        return None

    tx_datetime = parse_datetime(row_value(row, column_map, "tx_datetime"))
    confirm_amount = parse_decimal(row_value(row, column_map, "confirm_amount"))
    confirm_shares = parse_decimal(row_value(row, column_map, "confirm_shares"))
    if confirm_amount is None:
        confirm_amount = parse_decimal(row_value(row, column_map, "apply_amount"))
    if confirm_shares is None:
        confirm_shares = parse_decimal(row_value(row, column_map, "apply_shares"))
    if tx_datetime is None or confirm_amount is None or confirm_shares is None:
        return None

    return {
        "seq": seq,
        "page_number": page_number,
        "row_index": row_index,
        "order_id": clean_text(row_value(row, column_map, "order_id")),
        "tx_datetime": tx_datetime,
        "raw_type": raw_type,
        "code": code,
        "name": normalize_name(code, name),
        "apply_amount": parse_decimal(row_value(row, column_map, "apply_amount")),
        "apply_shares": parse_decimal(row_value(row, column_map, "apply_shares")),
        "confirm_amount": confirm_amount,
        "confirm_shares": confirm_shares,
        "fee": parse_decimal(row_value(row, column_map, "fee")) or Decimal("0"),
    }


def parse_pdf_metadata(pdf_file):
    metadata = {"source_scope": "default", "statement_id": None}
    with pdfplumber.open(pdf_file) as pdf:
        if not pdf.pages:
            return metadata
        text = pdf.pages[0].extract_text() or ""

    statement_match = re.search(r"编号：([^\n]+)", text)
    if statement_match:
        metadata["statement_id"] = statement_match.group(1).strip()

    return metadata


def extract_rows(pdf_file):
    rows = []
    with pdfplumber.open(pdf_file) as pdf:
        seq = 0
        for page_number, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables():
                column_map = dict(FALLBACK_COLUMNS)
                for row_index, row in enumerate(table):
                    if not row:
                        continue

                    column_map = detect_column_map(row, column_map)
                    parsed_row = extract_transaction_row(row, column_map, seq, page_number, row_index)
                    if parsed_row is None:
                        continue

                    rows.append(parsed_row)
                    seq += 1
    return rows


def classify_transaction(row, order_rows):
    raw_type = row["raw_type"]
    if raw_type in BUY_TYPES:
        return "BUY"
    if raw_type in SELL_TYPES:
        return "SELL"
    if "转换" in raw_type:
        if len(order_rows) == 1:
            return "BUY"
        apply_amount = row["apply_amount"] or Decimal("0")
        return "BUY" if apply_amount > 0 else "SELL"
    return None


def build_transactions(rows):
    rows = sorted(rows, key=lambda item: (item["tx_datetime"], item["seq"]))
    order_map = defaultdict(list)
    for row in rows:
        order_map[row["order_id"]].append(row)

    transactions = []
    for row in rows:
        tx_type = classify_transaction(row, order_map[row["order_id"]])
        if tx_type is None:
            continue

        amount = abs(row["confirm_amount"])
        shares = abs(row["confirm_shares"])
        nav = (amount / shares) if shares > 0 else Decimal("0")
        trade_time = row["tx_datetime"].strftime("%Y-%m-%dT%H:%M:%S")
        external_id = row["order_id"] or f"pdf-row-{row['seq']}"
        external_id = f"{external_id}:{row['code']}:{tx_type}:{amount:.2f}:{shares:.2f}"

        transactions.append(
            {
                "date": row["tx_datetime"].strftime("%Y-%m-%d"),
                "trade_time": trade_time,
                "code": row["code"],
                "name": row["name"],
                "type": tx_type,
                "amount": f"{amount:.2f}",
                "shares": f"{shares:.2f}",
                "nav": f"{nav:.4f}",
                "fee": f"{row['fee']:.2f}",
                "remark": f"PDF Import ({row['raw_type']})",
                "raw_type": row["raw_type"],
                "external_id": external_id,
            }
        )

    return transactions


def load_pdf_snapshot(pdf_file):
    metadata = parse_pdf_metadata(pdf_file)
    rows = extract_rows(pdf_file)
    transactions = build_transactions(rows)
    return metadata, transactions


def write_transactions(output_file, transactions):
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    fieldnames = ["date", "code", "name", "type", "amount", "shares", "nav", "fee", "remark"]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in transactions:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def import_pdf(pdf_file, output_file, backup=True):
    if not os.path.exists(pdf_file):
        raise FileNotFoundError(f"PDF not found: {pdf_file}")

    _, transactions = load_pdf_snapshot(pdf_file)
    if not transactions:
        raise RuntimeError("No valid transactions found in PDF.")

    backup_path = backup_existing_file(output_file) if backup else None
    write_transactions(output_file, transactions)
    return transactions, backup_path


def main():
    parser = argparse.ArgumentParser(description="Import Ant Fund PDF transactions into the ledger and CSV export.")
    parser.add_argument("pdf_file", help="Path to the source PDF.")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help="Path to the output CSV.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a timestamped backup before overwriting the CSV.",
    )
    parser.add_argument(
        "--legacy-csv-only",
        action="store_true",
        help="Only rebuild the CSV export without syncing the SQLite ledger.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Replace prior PDF rows for this scope instead of incrementally appending with de-duplication.",
    )
    args = parser.parse_args()

    if args.legacy_csv_only:
        transactions, backup_path = import_pdf(
            pdf_file=args.pdf_file,
            output_file=args.output,
            backup=not args.no_backup,
        )
        dates = [row["date"] for row in transactions]
        print(f"Imported {len(transactions)} transactions.")
        print(f"Date range: {min(dates)} -> {max(dates)}")
        if backup_path:
            print(f"Backup: {backup_path}")
        print(f"Output: {args.output}")
        return

    metadata, transactions = load_pdf_snapshot(args.pdf_file)
    backup_path = backup_existing_file(args.output) if not args.no_backup else None
    data_dir = os.path.dirname(args.output) or "."
    if args.snapshot:
        summary = replace_source_transactions(
            db_path=get_db_path(data_dir),
            csv_path=args.output,
            transactions=transactions,
            source_type="ant_pdf",
            source_scope=metadata.get("source_scope") or "default",
            drop_bootstrap=True,
        )
    else:
        summary = upsert_source_transactions(
            db_path=get_db_path(data_dir),
            csv_path=args.output,
            transactions=transactions,
            source_type="ant_pdf",
            source_scope=metadata.get("source_scope") or "default",
            dedupe_bootstrap=True,
        )

    dates = [row["date"] for row in transactions]
    print(f"Synced {len(transactions)} transactions into ledger.")
    print(f"Date range: {min(dates)} -> {max(dates)}")
    if args.snapshot:
        print(f"Replaced prior PDF rows: {summary['replaced']}")
        print(f"Dropped bootstrap rows: {summary['dropped_bootstrap']}")
    else:
        print(f"Skipped existing rows: {summary['skipped']}")
        print(f"Bootstrap rows replaced: {summary['bootstrap_replaced']}")
    if backup_path:
        print(f"Backup: {backup_path}")
    print(f"CSV export: {args.output}")
    print(f"DB: {get_db_path(data_dir)}")


if __name__ == "__main__":
    main()
