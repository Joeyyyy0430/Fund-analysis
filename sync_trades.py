import argparse
import os

from import_pdf_to_csv import load_pdf_snapshot
from trade_ledger import (
    fetch_transactions,
    get_db_path,
    replace_source_transactions,
    upsert_source_transactions,
)


DATA_DIR = "fund_data"
CSV_PATH = os.path.join(DATA_DIR, "transactions.csv")
DB_PATH = get_db_path(DATA_DIR)


def sync_pdf(pdf_file, source_scope=None, mode="incremental", drop_bootstrap=True):
    metadata, transactions = load_pdf_snapshot(pdf_file)
    if not transactions:
        raise RuntimeError("No valid transactions found in PDF.")

    resolved_scope = source_scope or metadata.get("source_scope") or "default"
    if mode == "snapshot":
        summary = replace_source_transactions(
            db_path=DB_PATH,
            csv_path=CSV_PATH,
            transactions=transactions,
            source_type="ant_pdf",
            source_scope=resolved_scope,
            drop_bootstrap=drop_bootstrap,
        )
    else:
        summary = upsert_source_transactions(
            db_path=DB_PATH,
            csv_path=CSV_PATH,
            transactions=transactions,
            source_type="ant_pdf",
            source_scope=resolved_scope,
            dedupe_bootstrap=True,
        )
    total_rows = len(fetch_transactions(DB_PATH, csv_path=CSV_PATH))
    return metadata, summary, total_rows


def main():
    parser = argparse.ArgumentParser(description="Sync trade records into the SQLite ledger.")
    parser.add_argument("input_file", help="Path to the source trade statement PDF.")
    parser.add_argument(
        "--source-scope",
        help="Override the account scope used for snapshot replacement.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Replace prior PDF rows for this scope instead of incrementally appending with de-duplication.",
    )
    parser.add_argument(
        "--keep-bootstrap",
        action="store_true",
        help="Keep legacy bootstrap rows instead of deleting them in snapshot mode.",
    )
    args = parser.parse_args()

    input_path = os.path.abspath(args.input_file)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    ext = os.path.splitext(input_path)[1].lower()
    if ext != ".pdf":
        raise ValueError("Only PDF snapshot sync is supported right now.")

    mode = "snapshot" if args.snapshot else "incremental"
    metadata, summary, total_rows = sync_pdf(
        input_path,
        source_scope=args.source_scope,
        mode=mode,
        drop_bootstrap=not args.keep_bootstrap,
    )

    print(f"Synced PDF ({mode}): {input_path}")
    print(f"Source scope: {args.source_scope or metadata.get('source_scope') or 'default'}")
    print(f"Inserted rows: {summary['inserted']}")
    if mode == "snapshot":
        print(f"Replaced prior PDF rows: {summary['replaced']}")
        print(f"Dropped bootstrap rows: {summary['dropped_bootstrap']}")
    else:
        print(f"Skipped existing rows: {summary['skipped']}")
        print(f"Bootstrap rows replaced: {summary['bootstrap_replaced']}")
    print(f"Ledger rows now: {total_rows}")
    print(f"DB: {DB_PATH}")
    print(f"CSV export: {CSV_PATH}")


if __name__ == "__main__":
    main()
