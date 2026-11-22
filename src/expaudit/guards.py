"""Guards: Python checks every claim in a model answer before it is used.

Any problem -> the caller drops ALL model findings and sends the line to NEEDS_HUMAN (Python
checks are kept). Problem messages never repeat memo or merchant text.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from expaudit.audit import log_problem
from expaudit.db import Line
from expaudit.packet import escape
from expaudit.schema import ModelAnswer

NUMBER_WORDS = frozenset({"one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                          "ten", "eleven", "twelve", "half", "couple", "plus"})

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z]+")
_SPACES = re.compile(r"\s+")


def numbers_in(text: str) -> set[Decimal]:
    """Every number in a text as Decimal ("$1,050.00" -> 1050, "2026-10-01" -> 2026, 10, 1)."""
    out = set()
    for token in _NUMBER.findall(text):
        try:
            out.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            pass
    return out


def _squash(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _has_number_word(text: str) -> bool:
    return any(w in NUMBER_WORDS for w in _WORD.findall(text.lower()))


def trusted_numbers(line: Line, retrieved: dict[str, dict]) -> set[Decimal]:
    """Numbers the explanation may use: line fields and the retrieved policy text only."""
    parts = [line.line_id, str(line.amount), line.transaction_date.isoformat()]
    parts += [str(v) for v in (line.nights, line.attendees_count) if v is not None]
    for c in retrieved.values():
        parts += [c["clause_id"], c["title"], c["text"]]
    return numbers_in(" ".join(parts))


def _memo_quote_ok(quote: str, memo: str) -> bool:
    q = _squash(quote)
    return bool(q) and (q in _squash(memo) or q in _squash(escape(memo)))


def check(answer: ModelAnswer, line: Line, retrieved: dict[str, dict],
          policy_ids: set[str]) -> list[str]:
    """Return the list of guard problems (empty = every claim checks out)."""
    problems: list[str] = []
    verified_quote_numbers: set[Decimal] = set()

    for n, f in enumerate(answer.findings, 1):
        tag = f"finding {n} ({f.code})"
        cited = [retrieved[cid] for cid in f.cited_clause_ids if cid in retrieved]
        for cid in f.cited_clause_ids:
            if cid not in policy_ids:
                problems.append(f"{tag}: cited clause {cid!r} does not exist in the policy")
            elif cid not in retrieved:
                problems.append(f"{tag}: cited clause {cid!r} was not retrieved in this run")

        quote = _squash(f.policy_quote)
        if not any(quote in _squash(c["text"]) for c in cited):
            problems.append(f"{tag}: policy_quote is not an exact substring of a cited clause")
        if f.limit_amount is not None and f.limit_amount not in numbers_in(f.policy_quote):
            problems.append(f"{tag}: limit_amount {f.limit_amount} does not appear in policy_quote")

        needs_memo = [name for name, v in (("memo_amount", f.memo_amount),
                                           ("attendees_count", f.attendees_count),
                                           ("flight_hours", f.flight_hours)) if v is not None]
        if f.memo_quote is None:
            for name in needs_memo:
                problems.append(f"{tag}: {name} given without a memo_quote")
            continue
        if not _memo_quote_ok(f.memo_quote, line.memo):
            problems.append(f"{tag}: memo_quote is not an exact substring of the memo")
            continue

        quote_numbers = numbers_in(f.memo_quote)
        quote_ok = True
        if f.memo_amount is not None and f.memo_amount not in quote_numbers:
            problems.append(f"{tag}: memo_amount {f.memo_amount} does not appear in memo_quote")
            quote_ok = False
        if f.attendees_count is not None and not (
                Decimal(f.attendees_count) in quote_numbers or _has_number_word(f.memo_quote)):
            problems.append(f"{tag}: attendees_count {f.attendees_count} not supported by memo_quote")
            quote_ok = False
        if f.flight_hours is not None and not (
                f.flight_hours in quote_numbers
                or Decimal(int(f.flight_hours)) in quote_numbers
                or _has_number_word(f.memo_quote)):
            problems.append(f"{tag}: flight_hours {f.flight_hours} not supported by memo_quote")
            quote_ok = False
        if quote_ok:
            verified_quote_numbers |= quote_numbers

    allowed = trusted_numbers(line, retrieved) | verified_quote_numbers
    for number in sorted(numbers_in(answer.explanation) - allowed):
        problems.append(f"explanation uses the number {number}, which is not from a line field, "
                        f"the retrieved policy text or a verified memo quote")
    return problems


def run_guards(answer: ModelAnswer, line: Line, retrieved: dict[str, dict],
               policy_ids: set[str]) -> list[str]:
    """check() + log each problem (line_id + message) to logs/expaudit.log."""
    problems = check(answer, line, retrieved, policy_ids)
    for p in problems:
        log_problem(line.line_id, f"guard: {p}")
    return problems
