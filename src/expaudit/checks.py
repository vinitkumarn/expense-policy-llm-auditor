"""Structured checks that need no reading. Always run by Python and never dropped."""

from __future__ import annotations

from expaudit import config
from expaudit.db import Line
from expaudit.decide import Finding


def missing_receipt(line: Line) -> Finding | None:
    """Policy 3.1: a receipt is required for any line OVER $75 ($75.00 itself is fine)."""
    if not line.receipt_attached and line.amount > config.RECEIPT_THRESHOLD:
        return Finding("MISSING_RECEIPT", cites=("3.1",))
    return None


_DUPLICATE_SQL = """
SELECT l.line_id
FROM expense_lines l
JOIN reports r ON r.report_id = l.report_id
WHERE r.employee_id = ? AND l.merchant = ? AND l.transaction_date = ? AND l.amount = ?
  AND (r.submitted_date < ? OR (r.submitted_date = ? AND l.line_id < ?))
ORDER BY r.submitted_date, l.line_id
LIMIT 1
"""


def duplicate(con, line: Line) -> Finding | None:
    """Policy 3.5: same employee, merchant, date and amount as an EARLIER line."""
    row = con.execute(_DUPLICATE_SQL, [
        line.employee_id, line.merchant, line.transaction_date, line.amount,
        line.submitted_date, line.submitted_date, line.line_id,
    ]).fetchone()
    if row is None:
        return None
    return Finding("DUPLICATE", cites=("3.5",))


def run_checks(con, line: Line) -> list[Finding]:
    return [f for f in (missing_receipt(line), duplicate(con, line)) if f is not None]
