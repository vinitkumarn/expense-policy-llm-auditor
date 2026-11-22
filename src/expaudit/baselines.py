"""Two baselines with no model and no policy reading.

naive: the Python structured checks only (MISSING_RECEIPT, DUPLICATE).
rules: naive + a hardcoded limits table (config.RULES_LIMITS, Appendix B values written before
       amendment C-1) + memo keyword lists + regexes for attendees and flight time.

The rules are written once from the policy wording, not tuned to the planted cases. They are
expected to miss things a reader of the policy would catch (C-1, reworded memos, red-team).
"""

from __future__ import annotations

import re
from decimal import Decimal

from expaudit import config
from expaudit.checks import run_checks
from expaudit.db import Line, load_line
from expaudit.decide import Decision, Finding, decide

_I = re.IGNORECASE

# Detection keywords and regexes (what the reworded set is designed to slip past).
ALCOHOL_RE = re.compile(r"\b(wine|beer|alcohol)\b", _I)
PERSONAL_RE = re.compile(r"\b(minibar|spa|movies?)\b", _I)
DINNER_RE = re.compile(r"\bdinner\b", _I)
OTHER_MEAL_RE = re.compile(r"\b(lunch|breakfast)\b", _I)
CLIENT_RE = re.compile(r"\bclient\b", _I)
BUSINESS_RE = re.compile(r"\bbusiness class\b", _I)
ATTENDEES_RE = re.compile(r"\b(\d+)\s+(people|persons|attendees|guests)\b", _I)   # "3 people"
FLIGHT_RE = re.compile(r"\b(\d+)h\s*(\d+)m\b", _I)                               # "5h 35m"
ECONOMY_FARE_RE = re.compile(r"economy fare (?:was |of |is )?\$(\d+(?:\.\d{2})?)", _I)

PATTERNS = [ALCOHOL_RE, PERSONAL_RE, DINNER_RE, OTHER_MEAL_RE, CLIENT_RE, BUSINESS_RE,
            ATTENDEES_RE, FLIGHT_RE, ECONOMY_FARE_RE]

# Generic helpers (not detectors): a dollar amount, and memo segments ("Dinner, incl. $18 beer.")
AMOUNT_RE = re.compile(r"\$(\d+(?:\.\d{2})?)")
SEGMENT_SPLIT_RE = re.compile(r"[,;:()]|\.(?!\d)")

# Fixed citations the rules attach (they cannot know about Appendix C).
CITES = {
    "dinner": ("4.2", "Appendix B"),
    "other_meal": ("4.1", "Appendix B"),
    "client": ("4.3",),
    "lodging": ("5.1", "Appendix B"),
    "ALCOHOL": ("4.5",),
    "PERSONAL_ITEM": ("7.1",),
    "AIRFARE_CLASS": ("6.1",),
}


def _tier(city: str) -> int:
    return 1 if city in config.RULES_TIER1_CITIES else 2


def _keyword_amount(memo: str, pattern: re.Pattern) -> tuple[bool, Decimal | None]:
    """(keyword found?, dollar amount in the same memo segment as the first hit, if any)."""
    found = False
    for segment in SEGMENT_SPLIT_RE.split(memo):
        if pattern.search(segment):
            found = True
            amount = AMOUNT_RE.search(segment)
            if amount:
                return True, Decimal(amount.group(1))
    return found, None


def naive(con, line: Line) -> tuple[list[Finding], list[str]]:
    return run_checks(con, line), []


def rules(con, line: Line) -> tuple[list[Finding], list[str]]:
    findings = run_checks(con, line)
    reasons: list[str] = []
    memo, tier = line.memo, _tier(line.city)

    if line.category == "Meals":
        client = bool(CLIENT_RE.search(memo))
        if client:
            kind = "client"
        elif DINNER_RE.search(memo):
            kind = "dinner"
        elif OTHER_MEAL_RE.search(memo):
            kind = "other_meal"
        else:
            kind = None

        has_alcohol, alcohol_amount = _keyword_amount(memo, ALCOHOL_RE)
        if has_alcohol and not client:          # 4.5 exception for client entertainment
            if alcohol_amount is None:
                reasons.append("alcohol mentioned but not itemized")
            else:
                findings.append(Finding("ALCOHOL", CITES["ALCOHOL"], memo_amount=alcohol_amount))

        if kind is not None:
            attendees = line.attendees_count
            match = ATTENDEES_RE.search(memo)
            if attendees is None and match:
                attendees = int(match.group(1))
            if attendees is None:
                reasons.append("attendees unknown for a group meal")
            else:
                findings.append(Finding("MEAL_OVER_LIMIT", CITES[kind],
                                        limit_amount=config.RULES_LIMITS[(kind, tier)],
                                        attendees_count=attendees))

    has_personal, personal_amount = _keyword_amount(memo, PERSONAL_RE)
    if has_personal:
        findings.append(Finding("PERSONAL_ITEM", CITES["PERSONAL_ITEM"],
                                memo_amount=personal_amount))

    if line.category == "Lodging" and line.nights is not None:
        findings.append(Finding("LODGING_OVER_LIMIT", CITES["lodging"],
                                limit_amount=config.RULES_LIMITS[("lodging", tier)]))

    if line.category == "Airfare" and BUSINESS_RE.search(memo):
        flight = FLIGHT_RE.search(memo)
        if flight is None:
            reasons.append("business class without a flight time")
        else:
            hours = Decimal(flight.group(1)) + Decimal(flight.group(2)) / 60
            if hours <= config.RULES_BUSINESS_CLASS_MIN_HOURS:
                fare = ECONOMY_FARE_RE.search(memo)
                findings.append(Finding("AIRFARE_CLASS", CITES["AIRFARE_CLASS"],
                                        memo_amount=Decimal(fare.group(1)) if fare else None))

    return findings, reasons


BASELINES = {"naive": naive, "rules": rules}


def audit(con, line_id: str, by: str) -> Decision:
    """Run one auditor on one line and let decide() make every decision."""
    if by not in BASELINES:
        raise ValueError(f"unknown auditor {by!r}; choose from {sorted(BASELINES)}")
    line = load_line(con, line_id)
    findings, reasons = BASELINES[by](con, line)
    return decide(line, findings, reasons)
