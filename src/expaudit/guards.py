"""Guards: Python checks every claim in a model answer before it is used.

Two kinds, split by what they protect:
- Decision guards (cited ids, policy_quote, limit_amount, memo_quote, memo_amount, attendees,
  flight_hours, client_meal_quote, attendee_names_quote). Any problem -> the caller drops ALL model findings and sends the line to
  NEEDS_HUMAN (Python checks are kept).
- The explanation-number check. The explanation is display text only, so a failure does not
  change the decision: the explanation is replaced with HIDDEN_EXPLANATION, logged and counted.
Problem messages never repeat memo or merchant text.
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

CLIENT_CLAUSE = "4.3"   # client entertainment (limit per person); decide.py uses the same id

HIDDEN_EXPLANATION ="Explanation hidden: it contained a number that could not be verified."


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


def _check(answer: ModelAnswer, line: Line, retrieved: dict[str, dict],
           policy_ids: set[str]) -> tuple[list[str], set[Decimal]]:
    """Decision-guard problems, plus the numbers from memo quotes that fully verified."""
    problems: list[str] = []
    verified_quote_numbers: set[Decimal] = set()

    # Client meal (answer level). Python decides 4.3 / 4.4 from these in decide.py.
    if answer.client_meal:
        if answer.client_meal_quote is None:
            problems.append("client_meal given without a client_meal_quote")
        elif not _memo_quote_ok(answer.client_meal_quote, line.memo):
            problems.append("client_meal_quote is not an exact substring of the memo")
    if (answer.attendee_names_quote is not None
            and not _memo_quote_ok(answer.attendee_names_quote, line.memo)):
        problems.append("attendee_names_quote is not an exact substring of the memo")

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
        # A client meal that cites 4.3 must take its limit from 4.3, not from another cited clause.
        if (answer.client_meal and f.code == "MEAL_OVER_LIMIT" and CLIENT_CLAUSE in retrieved
                and CLIENT_CLAUSE in f.cited_clause_ids
                and quote not in _squash(retrieved[CLIENT_CLAUSE]["text"])):
            problems.append(f"{tag}: client meal cites {CLIENT_CLAUSE} but policy_quote is not "
                            f"from {CLIENT_CLAUSE}")

        # attendees_count equal to the line's own field is already trusted.
        attendees_from_line = (f.attendees_count is not None
                               and f.attendees_count == line.attendees_count)
        attendees = None if attendees_from_line else f.attendees_count
        needs_memo = [name for name, v in (("memo_amount", f.memo_amount),
                                           ("attendees_count", attendees),
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
        if attendees is not None and not (
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

    return problems, verified_quote_numbers


def check(answer: ModelAnswer, line: Line, retrieved: dict[str, dict],
          policy_ids: set[str]) -> list[str]:
    """Decision-guard problems (empty = every claim the decision uses checks out)."""
    return _check(answer, line, retrieved, policy_ids)[0]


def explanation_problems(answer: ModelAnswer, line: Line, retrieved: dict[str, dict],
                         policy_ids: set[str]) -> list[str]:
    """Numbers in the explanation that are not from a line field, the retrieved policy text or
    a verified memo quote. These never change the decision."""
    verified = _check(answer, line, retrieved, policy_ids)[1]
    allowed = trusted_numbers(line, retrieved) | verified
    return [f"explanation uses the number {number}, which is not from a line field, "
            f"the retrieved policy text or a verified memo quote"
            for number in sorted(numbers_in(answer.explanation) - allowed)]


def run_guards(answer: ModelAnswer, line: Line, retrieved: dict[str, dict],
               policy_ids: set[str]) -> tuple[list[str], ModelAnswer, bool]:
    """Run both kinds of guard and log each problem (line_id + message) to logs/expaudit.log.

    Returns (decision problems, the answer to use, explanation_hidden). With decision problems
    the line goes to NEEDS_HUMAN, so the explanation is not checked. Otherwise an unverified
    explanation number replaces the explanation with HIDDEN_EXPLANATION; the findings stand.
    """
    problems = check(answer, line, retrieved, policy_ids)
    for p in problems:
        log_problem(line.line_id, f"guard: {p}")
    if problems:
        return problems, answer, False
    hidden = explanation_problems(answer, line, retrieved, policy_ids)
    for p in hidden:
        log_problem(line.line_id, f"explanation hidden: {p}")
    if hidden:
        answer = answer.model_copy(update={"explanation": HIDDEN_EXPLANATION})
    return problems, answer, bool(hidden)
