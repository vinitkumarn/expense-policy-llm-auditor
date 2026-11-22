"""The input packet for one expense line (both model paths).

Line fields + report status + employee grade. Every free-text field (merchant, memo) is
UNTRUSTED: < and > are escaped, then it is wrapped as <UNTRUSTED>...</UNTRUSTED>, so a memo can
never close the wrapper or open a fake tag. The fixed path also appends the retrieved clauses.
The packet text is deterministic: it is part of the response cache key.
"""

from __future__ import annotations

from expaudit import config
from expaudit.db import Line


def escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def untrusted(text: str) -> str:
    return f"<UNTRUSTED>{escape(text)}</UNTRUSTED>"


def _blank(value) -> str:
    return "(blank)" if value is None else str(value)


def line_fields(line: Line) -> str:
    rows = [
        ("line_id", line.line_id),
        ("transaction_date", line.transaction_date.isoformat()),
        ("category", line.category),
        ("amount", str(line.amount)),
        ("currency", config.CURRENCY),
        ("city", line.city),
        ("nights", _blank(line.nights)),
        ("attendees_count", _blank(line.attendees_count)),
        ("receipt_attached", "Y" if line.receipt_attached else "N"),
        ("report_status", line.report_status),
        ("employee_grade", line.grade),
        ("merchant", untrusted(line.merchant)),
        ("memo", untrusted(line.memo)),
    ]
    return "\n".join(f"{name}: {value}" for name, value in rows)


def build_packet(line: Line, clauses: list[dict] | None = None) -> str:
    parts = ["Expense line:", line_fields(line)]
    if clauses is not None:
        parts.append("")
        parts.append("Retrieved policy clauses (the only clauses you may cite):")
        for c in clauses:
            parts.append(f"[{c['clause_id']}] {c['title']}\n{c['text']}")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"
