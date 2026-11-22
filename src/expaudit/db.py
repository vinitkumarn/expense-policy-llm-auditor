"""Load the CSVs into DuckDB with real types, and read one expense line back.

Money is DECIMAL(18,2) in the database and Decimal in Python. Dates are DATE / datetime.date.
Blank CSV cells become NULL (Python None).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

from expaudit import config

DB_FILE = "expaudit.duckdb"

# Column name -> DuckDB type, in CSV column order.
TABLES = {
    "employees": {
        "employee_id": "VARCHAR", "name": "VARCHAR", "grade": "VARCHAR",
        "manager_id": "VARCHAR", "cost_center": "VARCHAR",
    },
    "reports": {
        "report_id": "VARCHAR", "employee_id": "VARCHAR", "trip_purpose": "VARCHAR",
        "submitted_date": "DATE", "status": "VARCHAR", "paid_date": "DATE",
    },
    "expense_lines": {
        "line_id": "VARCHAR", "report_id": "VARCHAR", "transaction_date": "DATE",
        "merchant": "VARCHAR", "category": "VARCHAR", "amount": "DECIMAL(18,2)",
        "currency": "VARCHAR", "city": "VARCHAR", "nights": "INTEGER",
        "attendees_count": "INTEGER", "receipt_attached": "VARCHAR", "memo": "VARCHAR",
    },
}


@dataclass(frozen=True)
class Line:
    """One expense line joined with its report and employee."""
    line_id: str
    report_id: str
    employee_id: str
    grade: str                 # STAFF / MANAGER / DIRECTOR
    report_status: str         # SUBMITTED / PAID
    submitted_date: date
    transaction_date: date
    merchant: str              # UNTRUSTED free text
    category: str
    amount: Decimal
    city: str
    nights: int | None
    attendees_count: int | None
    receipt_attached: bool
    memo: str                  # UNTRUSTED free text


def build_db(csv_dir: Path, db_path: Path | str) -> duckdb.DuckDBPyConnection:
    """(Re)create the three tables from the CSVs. Returns an open read-write connection."""
    con = duckdb.connect(str(db_path))
    for table, columns in TABLES.items():
        csv_path = (Path(csv_dir) / f"{table}.csv").as_posix()
        con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(
            f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, header = true, "
            f"columns = {_struct(columns)}, nullstr = '', quote = '\"', escape = '\"')",
            [csv_path],
        )
    return con


def _struct(columns: dict[str, str]) -> str:
    return "{" + ", ".join(f"'{name}': '{typ}'" for name, typ in columns.items()) + "}"


def connect(db_path: Path | str = config.DB_PATH, read_only: bool = True):
    if not Path(db_path).exists():
        raise FileNotFoundError(f"{db_path} not found. Run: python -m expaudit build")
    return duckdb.connect(str(db_path), read_only=read_only)


_LINE_SQL = """
SELECT l.line_id, l.report_id, r.employee_id, e.grade, r.status, r.submitted_date,
       l.transaction_date, l.merchant, l.category, l.amount, l.city, l.nights,
       l.attendees_count, l.receipt_attached, l.memo
FROM expense_lines l
JOIN reports r ON r.report_id = l.report_id
JOIN employees e ON e.employee_id = r.employee_id
WHERE l.line_id = ?
"""


def load_line(con, line_id: str) -> Line:
    row = con.execute(_LINE_SQL, [line_id]).fetchone()
    if row is None:
        raise KeyError(f"no expense line {line_id!r}")
    (lid, rid, emp, grade, status, submitted, txn, merchant, category, amount, city, nights,
     attendees, receipt, memo) = row
    return Line(
        line_id=lid, report_id=rid, employee_id=emp, grade=grade, report_status=status,
        submitted_date=submitted, transaction_date=txn, merchant=merchant or "",
        category=category, amount=amount, city=city, nights=nights,
        attendees_count=attendees, receipt_attached=(receipt == "Y"), memo=memo or "",
    )
