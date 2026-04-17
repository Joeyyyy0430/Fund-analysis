import csv
import os
import sqlite3
import uuid
from datetime import datetime


DEFAULT_DB_FILENAME = "trades.db"
LEGACY_CSV_FIELDS = ["date", "code", "name", "type", "amount", "shares", "nav", "fee", "remark"]


def get_db_path(data_dir):
    return os.path.join(data_dir, DEFAULT_DB_FILENAME)


def utcnow_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_scope TEXT NOT NULL,
            external_id TEXT NOT NULL,
            trade_time TEXT,
            confirm_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            amount TEXT NOT NULL,
            shares TEXT NOT NULL,
            nav TEXT NOT NULL,
            fee TEXT NOT NULL DEFAULT '0',
            remark TEXT NOT NULL DEFAULT '',
            raw_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_type, source_scope, external_id)
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_confirm_date
        ON transactions(confirm_date, trade_time, id);

        CREATE INDEX IF NOT EXISTS idx_transactions_code
        ON transactions(code, confirm_date, trade_time, id);
        """
    )


def ledger_count(conn):
    row = conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()
    return int(row["count"])


def coerce_text(value, default=""):
    if value is None:
        return default
    return str(value)


def normalize_trade_time(value):
    text = coerce_text(value).strip()
    if not text:
        return None
    return text.replace(" ", "T")


def normalize_transaction(tx, source_type, source_scope, external_id):
    now = utcnow_iso()
    confirm_date = coerce_text(tx.get("date") or tx.get("confirm_date")).strip()
    if not confirm_date:
        raise ValueError("Transaction is missing confirm date.")

    trade_time = normalize_trade_time(tx.get("trade_time"))
    tx_type = coerce_text(tx.get("type")).strip().upper()
    if tx_type not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported transaction type: {tx_type}")

    return (
        source_type,
        source_scope,
        coerce_text(external_id).strip(),
        trade_time,
        confirm_date,
        coerce_text(tx.get("code")).strip(),
        coerce_text(tx.get("name")).strip(),
        tx_type,
        coerce_text(tx.get("amount")).strip(),
        coerce_text(tx.get("shares")).strip(),
        coerce_text(tx.get("nav")).strip(),
        coerce_text(tx.get("fee"), "0").strip() or "0",
        coerce_text(tx.get("remark")).strip(),
        coerce_text(tx.get("raw_type")).strip() or None,
        now,
        now,
    )


def bootstrap_from_csv(conn, csv_path):
    if not os.path.exists(csv_path):
        return 0

    inserted = 0
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for index, row in enumerate(reader, start=1):
            row = dict(row)
            row["trade_time"] = row.get("date")
            rows.append(
                normalize_transaction(
                    row,
                    source_type="bootstrap",
                    source_scope="legacy_csv",
                    external_id=f"legacy-row-{index}",
                )
            )

    if rows:
        conn.executemany(
            """
            INSERT INTO transactions (
                source_type, source_scope, external_id, trade_time, confirm_date,
                code, name, type, amount, shares, nav, fee, remark, raw_type,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        inserted = len(rows)
    return inserted


def ensure_ledger(db_path, csv_path=None):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with connect(db_path) as conn:
        ensure_schema(conn)
        if ledger_count(conn) == 0 and csv_path and os.path.exists(csv_path):
            bootstrap_from_csv(conn, csv_path)


def fetch_transactions(db_path, csv_path=None):
    ensure_ledger(db_path, csv_path=csv_path)
    with connect(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT
                confirm_date,
                code,
                name,
                type,
                amount,
                shares,
                nav,
                fee,
                remark,
                raw_type,
                trade_time,
                source_type,
                source_scope,
                external_id
            FROM transactions
            ORDER BY confirm_date, COALESCE(trade_time, confirm_date), id
            """
        ).fetchall()

    transactions = []
    for row in rows:
        transactions.append(
            {
                "date": row["confirm_date"],
                "code": row["code"],
                "name": row["name"],
                "type": row["type"],
                "amount": row["amount"],
                "shares": row["shares"],
                "nav": row["nav"],
                "fee": row["fee"],
                "remark": row["remark"],
                "raw_type": row["raw_type"],
                "trade_time": row["trade_time"],
                "source_type": row["source_type"],
                "source_scope": row["source_scope"],
                "external_id": row["external_id"],
            }
        )
    return transactions


def export_transactions_csv(db_path, csv_path):
    transactions = fetch_transactions(db_path)
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEGACY_CSV_FIELDS)
        writer.writeheader()
        for row in transactions:
            writer.writerow({field: row.get(field, "") for field in LEGACY_CSV_FIELDS})
    return len(transactions)


def append_manual_transaction(db_path, csv_path, tx_data):
    ensure_ledger(db_path, csv_path=csv_path)
    row = dict(tx_data)
    row["trade_time"] = row.get("trade_time") or row.get("date")
    external_id = f"manual-{uuid.uuid4()}"

    with connect(db_path) as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO transactions (
                source_type, source_scope, external_id, trade_time, confirm_date,
                code, name, type, amount, shares, nav, fee, remark, raw_type,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalize_transaction(
                row,
                source_type="manual",
                source_scope="default",
                external_id=external_id,
            ),
        )

    export_transactions_csv(db_path, csv_path)
    return external_id


def _delete_one_bootstrap_match(conn, tx):
    row = conn.execute(
        """
        SELECT id
        FROM transactions
        WHERE source_type = 'bootstrap'
          AND confirm_date = ?
          AND code = ?
          AND type = ?
          AND amount = ?
          AND shares = ?
          AND nav = ?
        ORDER BY id
        LIMIT 1
        """,
        (
            coerce_text(tx.get("date") or tx.get("confirm_date")).strip(),
            coerce_text(tx.get("code")).strip(),
            coerce_text(tx.get("type")).strip().upper(),
            coerce_text(tx.get("amount")).strip(),
            coerce_text(tx.get("shares")).strip(),
            coerce_text(tx.get("nav")).strip(),
        ),
    ).fetchone()
    if not row:
        return 0

    conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
    return 1


def upsert_source_transactions(
    db_path,
    csv_path,
    transactions,
    source_type,
    source_scope="default",
    dedupe_bootstrap=True,
):
    ensure_ledger(db_path, csv_path=csv_path)

    inserted = 0
    skipped = 0
    bootstrap_replaced = 0

    with connect(db_path) as conn:
        ensure_schema(conn)

        for index, tx in enumerate(transactions, start=1):
            tx = dict(tx)
            external_id = tx.get("external_id") or f"{source_type}-{index}"
            normalized = normalize_transaction(
                tx,
                source_type=source_type,
                source_scope=source_scope,
                external_id=external_id,
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO transactions (
                    source_type, source_scope, external_id, trade_time, confirm_date,
                    code, name, type, amount, shares, nav, fee, remark, raw_type,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1

            if dedupe_bootstrap:
                bootstrap_replaced += _delete_one_bootstrap_match(conn, tx)

    export_transactions_csv(db_path, csv_path)
    return {
        "inserted": inserted,
        "skipped": skipped,
        "bootstrap_replaced": bootstrap_replaced,
    }


def replace_source_transactions(
    db_path,
    csv_path,
    transactions,
    source_type,
    source_scope="default",
    drop_bootstrap=True,
):
    ensure_ledger(db_path, csv_path=csv_path)

    with connect(db_path) as conn:
        ensure_schema(conn)

        previous_count = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions WHERE source_type = ? AND source_scope = ?",
            (source_type, source_scope),
        ).fetchone()["count"]

        bootstrap_count = 0
        if drop_bootstrap:
            bootstrap_count = conn.execute(
                "SELECT COUNT(*) AS count FROM transactions WHERE source_type = 'bootstrap'"
            ).fetchone()["count"]

        conn.execute(
            "DELETE FROM transactions WHERE source_type = ? AND source_scope = ?",
            (source_type, source_scope),
        )
        if drop_bootstrap:
            conn.execute("DELETE FROM transactions WHERE source_type = 'bootstrap'")

        rows = []
        for index, tx in enumerate(transactions, start=1):
            tx = dict(tx)
            external_id = tx.get("external_id") or f"{source_type}-{index}"
            rows.append(
                normalize_transaction(
                    tx,
                    source_type=source_type,
                    source_scope=source_scope,
                    external_id=external_id,
                )
            )

        conn.executemany(
            """
            INSERT INTO transactions (
                source_type, source_scope, external_id, trade_time, confirm_date,
                code, name, type, amount, shares, nav, fee, remark, raw_type,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    export_transactions_csv(db_path, csv_path)
    return {
        "inserted": len(transactions),
        "replaced": int(previous_count),
        "dropped_bootstrap": int(bootstrap_count),
    }
