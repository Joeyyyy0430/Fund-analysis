import csv
import os
import re
import sqlite3
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from functools import lru_cache
from io import StringIO

import pandas as pd
import requests


DEFAULT_DB_FILENAME = "trades.db"
LEGACY_CSV_FIELDS = ["date", "code", "name", "type", "amount", "shares", "nav", "fee", "remark"]
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://fundf10.eastmoney.com/",
}
MANUAL_TRANSACTION_EXCLUSIONS = {
    # Reconciled against the official holding snapshot: keeping this row inflates
    # 015686's remaining shares by exactly 26.02 and breaks holding-level P/L.
    "20260309001080012204770015338985:015686:BUY:100.00:26.02",
}


def get_db_path(data_dir):
    return os.path.join(data_dir, DEFAULT_DB_FILENAME)


def utcnow_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


def round_money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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


def _trade_day(text):
    normalized = normalize_trade_time(text)
    if not normalized:
        return ""
    return normalized[:10].replace("-", "")


def _external_id_day(external_id):
    prefix = coerce_text(external_id).split(":", 1)[0]
    digits = "".join(ch for ch in prefix if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _row_rank(row):
    trade_day = _trade_day(row.get("trade_time"))
    confirm_day = coerce_text(row.get("date") or row.get("confirm_date")).replace("-", "")
    external_day = _external_id_day(row.get("external_id"))
    source_scope = coerce_text(row.get("source_scope"))
    return (
        1 if source_scope == "default" else 0,
        1 if trade_day and trade_day == confirm_day else 0,
        1 if external_day and external_day == confirm_day else 0,
        1 if not coerce_text(row.get("trade_time")).endswith("T00:00:00") else 0,
        len(coerce_text(row.get("external_id"))),
    )


def _is_suspicious_scope_orphan(row):
    if coerce_text(row.get("source_type")) != "ant_pdf":
        return False
    if coerce_text(row.get("source_scope")) == "default":
        return False

    trade_day = _trade_day(row.get("trade_time"))
    confirm_day = coerce_text(row.get("date") or row.get("confirm_date")).replace("-", "")
    external_day = _external_id_day(row.get("external_id"))
    midnight_only = coerce_text(row.get("trade_time")).endswith("T00:00:00")

    # Old imports left a few PDF rows in non-default scopes with synthetic
    # external ids and midnight-only timestamps. They are not trustworthy and
    # should not affect holdings.
    return midnight_only and external_day and external_day != confirm_day


def _structural_trade_key(row):
    return (
        coerce_text(row.get("date") or row.get("confirm_date")),
        normalize_trade_time(row.get("trade_time")),
        coerce_text(row.get("code")).strip(),
        coerce_text(row.get("type")).strip().upper(),
        coerce_text(row.get("amount")).strip(),
        coerce_text(row.get("shares")).strip(),
        coerce_text(row.get("nav")).strip(),
        coerce_text(row.get("raw_type")).strip(),
    )


def canonicalize_transactions(transactions):
    by_trade_key = {}
    other_rows = []

    for row in transactions:
        row = dict(row)
        external_id = coerce_text(row.get("external_id")).strip()
        if external_id in MANUAL_TRANSACTION_EXCLUSIONS:
            continue

        if coerce_text(row.get("source_type")) == "ant_pdf" and external_id:
            trade_key = _structural_trade_key(row)
            existing = by_trade_key.get(trade_key)
            if existing is None or _row_rank(row) > _row_rank(existing):
                by_trade_key[trade_key] = row
            continue

        other_rows.append(row)

    canonical_rows = other_rows + list(by_trade_key.values())
    canonical_rows = [row for row in canonical_rows if not _is_suspicious_scope_orphan(row)]
    canonical_rows.sort(
        key=lambda row: (
            coerce_text(row.get("date") or row.get("confirm_date")),
            normalize_trade_time(row.get("trade_time")) or coerce_text(row.get("date") or row.get("confirm_date")),
            coerce_text(row.get("external_id")),
        )
    )
    return canonical_rows


def parse_trade_date(value):
    text = coerce_text(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def parse_rate_text(value):
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", coerce_text(value))
    if not match:
        return None
    return float(match.group(1)) / 100.0


def parse_holding_period_text(value):
    text = re.sub(r"\s+", "", coerce_text(value))
    if not text:
        return None

    min_days = 0
    max_days = None

    match = re.search(r"大于等于(\d+)天", text)
    if match:
        min_days = int(match.group(1))
    else:
        match = re.search(r"大于(\d+)天", text)
        if match:
            min_days = int(match.group(1)) + 1
        else:
            match = re.search(r"(\d+)天以上", text)
            if match:
                min_days = int(match.group(1))

    match = re.search(r"小于等于(\d+)天", text)
    if match:
        max_days = int(match.group(1)) + 1
    else:
        match = re.search(r"小于(\d+)天", text)
        if match:
            max_days = int(match.group(1))

    if min_days == 0 and max_days is None and "天" not in text:
        return None

    return {"label": text, "min_days": min_days, "max_days": max_days}


@lru_cache(maxsize=256)
def get_redemption_fee_rules(code):
    normalized_code = coerce_text(code).strip()
    if not re.fullmatch(r"\d{6}", normalized_code):
        return []

    url = f"https://fundf10.eastmoney.com/jjfl_{normalized_code}.html"
    try:
        response = requests.get(url, headers=EASTMONEY_HEADERS, timeout=5)
        response.raise_for_status()
        response.encoding = "utf-8"
        tables = pd.read_html(StringIO(response.text))
    except Exception:
        return []

    for table in tables:
        period_col = next((col for col in table.columns if "适用期限" in str(col)), None)
        rate_col = next((col for col in table.columns if "赎回费率" in str(col)), None)
        if period_col is None or rate_col is None:
            continue

        rules = []
        for _, row in table[[period_col, rate_col]].iterrows():
            period = parse_holding_period_text(row[period_col])
            rate = parse_rate_text(row[rate_col])
            if period is None or rate is None:
                continue
            rules.append(
                {
                    "label": period["label"],
                    "min_days": period["min_days"],
                    "max_days": period["max_days"],
                    "rate": rate,
                }
            )

        if rules:
            return sorted(
                rules,
                key=lambda item: (item["min_days"], float("inf") if item["max_days"] is None else item["max_days"]),
            )

    return []


def resolve_redemption_fee_rate(rules, holding_days):
    for rule in rules:
        if holding_days < rule["min_days"]:
            continue
        if rule["max_days"] is not None and holding_days >= rule["max_days"]:
            continue
        return rule["rate"]
    return 0.0


def enrich_transactions_with_fee_logic(transactions):
    positions = {}
    holding_lots = {}
    enriched = []

    for tx in canonicalize_transactions(transactions):
        row = dict(tx)
        code = coerce_text(row.get("code")).strip()
        tx_type = coerce_text(row.get("type")).strip().upper()

        if code not in positions:
            positions[code] = {"shares": 0.0, "cost": 0.0}
            holding_lots[code] = []

        try:
            shares = float(row.get("shares") or 0)
            amount = float(row.get("amount") or 0)
            recorded_fee = float(row.get("fee") or 0)
        except (TypeError, ValueError):
            row["effective_fee"] = 0.0
            row["fee_source"] = "invalid"
            enriched.append(row)
            continue

        effective_fee = recorded_fee
        fee_source = "recorded" if recorded_fee > 0 else "recorded_zero"

        if tx_type == "BUY":
            positions[code]["shares"] += shares
            positions[code]["cost"] += amount + recorded_fee
            holding_lots[code].append({"shares": shares, "date": row.get("date")})
        elif tx_type == "SELL" and shares > 0:
            rules = get_redemption_fee_rules(code)
            if recorded_fee <= 0 and rules:
                sell_date = parse_trade_date(row.get("date"))
                sell_nav = amount / shares if shares else 0.0
                estimated_fee = 0.0
                remaining = shares

                for lot in holding_lots[code]:
                    if remaining <= 1e-9:
                        break
                    lot_shares = float(lot["shares"])
                    if lot_shares <= 1e-9:
                        continue

                    matched_shares = min(remaining, lot_shares)
                    lot_date = parse_trade_date(lot.get("date"))
                    holding_days = max((sell_date - lot_date).days, 0) if sell_date and lot_date else 0
                    rate = resolve_redemption_fee_rate(rules, holding_days)
                    estimated_fee += matched_shares * sell_nav * rate
                    remaining -= matched_shares

                effective_fee = round_money(estimated_fee)
                fee_source = "estimated"

            remaining = shares
            while remaining > 1e-9 and holding_lots[code]:
                lot = holding_lots[code][0]
                matched_shares = min(remaining, lot["shares"])
                lot["shares"] -= matched_shares
                remaining -= matched_shares
                if lot["shares"] <= 1e-9:
                    holding_lots[code].pop(0)

            if positions[code]["shares"] > 0:
                avg_cost = positions[code]["cost"] / positions[code]["shares"]
                cost_part = shares * avg_cost
                positions[code]["shares"] -= shares
                positions[code]["cost"] -= cost_part

        row["effective_fee"] = effective_fee
        row["fee_source"] = fee_source
        enriched.append(row)

    return enriched


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
    return canonicalize_transactions(transactions)


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
