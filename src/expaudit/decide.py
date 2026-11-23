"""Python owns every decision: dollars, difference type, approver, actions, status.

decide() takes findings whose limit and memo amounts were already checked by the caller
(Python checks, the rules baseline, or a model answer that passed every guard). It never reads
the policy. Thresholds come from config.py (policy 8.1 / 8.2; Finance owns them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from expaudit import config
from expaudit.db import Line

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# Code -> difference type (design "Finding codes" table).
TIMING, ERROR, PERMANENT = "TIMING", "ERROR", "PERMANENT"
DIFFERENCE_TYPE = {
    "MISSING_RECEIPT": TIMING,
    "DUPLICATE": ERROR,
    "MISCATEGORIZED": ERROR,
    "MEAL_OVER_LIMIT": PERMANENT,
    "ALCOHOL": PERMANENT,
    "LODGING_OVER_LIMIT": PERMANENT,
    "PERSONAL_ITEM": PERMANENT,
    "AIRFARE_CLASS": PERMANENT,
}
PYTHON_CODES = frozenset({"MISSING_RECEIPT", "DUPLICATE"})   # always kept (never-drop)
FINANCE_CODES = frozenset({"ALCOHOL", "PERSONAL_ITEM"})      # policy 8.1

# Actions (policy 8.2). ACTION_ORDER is the order actions are listed in.
REJECT = "REJECT_LINE_BEFORE_PAYMENT"
RECOVER = "RECOVER_FROM_EMPLOYEE"
HOLD = "HOLD_LINE_FOR_RECEIPT"
REQUEST_RECEIPT = "REQUEST_RECEIPT_AFTER_PAYMENT"
RECLASS = "RECLASS"
ACTION_ORDER = [REJECT, RECOVER, HOLD, REQUEST_RECEIPT, RECLASS]

CLIENT_CLAUSE = "4.3"   # client entertainment limit (policy 4.3; names required by 4.4)
ATTENDEES_MISSING = "attendee count missing (4.4)"

# A limit finding should cite the rule clause (4.2, 5.1, ...) as well as the appendix that holds
# the number. Citing only appendices adds this note; the decision itself is unchanged.
LIMIT_CODES = ("MEAL_OVER_LIMIT", "LODGING_OVER_LIMIT")
CITATION_INCOMPLETE = "citation incomplete"

OK, NEEDS_HUMAN = "OK", "NEEDS_HUMAN"
MANAGER, FINANCE, NO_APPROVER = "MANAGER", "FINANCE", "NONE"


@dataclass(frozen=True)
class Finding:
    """One finding on one line. Amounts are already verified by whoever produced it.

    limit_amount: per-person / per-night limit (MEAL_OVER_LIMIT, LODGING_OVER_LIMIT).
    memo_amount: the dollar amount quoted from the memo (alcohol, personal item, economy fare).
    attendees_count: from the memo, used when the line field is blank.
    flight_hours: scheduled flight time from the memo (AIRFARE_CLASS). For AIRFARE_CLASS,
        limit_amount is the policy's hour threshold (6.1) and Python does the comparison.
    """
    code: str
    cites: tuple[str, ...] = ()
    limit_amount: Decimal | None = None
    memo_amount: Decimal | None = None
    attendees_count: int | None = None
    flight_hours: Decimal | None = None


@dataclass
class Decision:
    line_id: str
    codes: list[str]
    cites: dict[str, list[str]]
    dollars_by_code: dict[str, Decimal | None]
    difference_types: dict[str, str]
    dollars_out_of_policy: Decimal | None   # PERMANENT + ERROR; None = unknown (never 0.00)
    dollars_held: Decimal                   # TIMING; never added to dollars_out_of_policy
    approver: str | None
    actions: list[str]
    status: str
    needs_human_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # review notes; never change the status

    def to_dict(self) -> dict:
        def money(v):
            return None if v is None else str(v)
        return {
            "line_id": self.line_id, "codes": self.codes, "cites": self.cites,
            "dollars_by_code": {k: money(v) for k, v in self.dollars_by_code.items()},
            "difference_types": self.difference_types,
            "dollars_out_of_policy": money(self.dollars_out_of_policy),
            "dollars_held": money(self.dollars_held),
            "approver": self.approver, "actions": self.actions, "status": self.status,
            "needs_human_reasons": self.needs_human_reasons,
            "notes": self.notes,
        }


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def _dollars(f: Finding, line: Line, by_code: dict[str, Finding]) -> Decimal | None:
    """Dollars out of policy for one finding (design formulas). None = cannot be computed."""
    code = f.code
    if code in ("MISSING_RECEIPT", "MISCATEGORIZED"):
        return ZERO
    if code == "DUPLICATE":
        return line.amount
    if code == "ALCOHOL":
        return f.memo_amount
    if code == "PERSONAL_ITEM":
        return f.memo_amount if f.memo_amount is not None else line.amount
    if code == "AIRFARE_CLASS":
        return None if f.memo_amount is None else line.amount - f.memo_amount
    if code == "MEAL_OVER_LIMIT":
        attendees = f.attendees_count if f.attendees_count is not None else line.attendees_count
        alcohol = by_code.get("ALCOHOL")
        if f.limit_amount is None or attendees is None or (alcohol and alcohol.memo_amount is None):
            return None
        base = line.amount - (alcohol.memo_amount if alcohol else ZERO)
        return max(ZERO, base - f.limit_amount * attendees)
    if code == "LODGING_OVER_LIMIT":
        personal = by_code.get("PERSONAL_ITEM")
        if f.limit_amount is None or line.nights is None:
            return None
        personal_amount = ZERO
        if personal:
            personal_amount = (personal.memo_amount if personal.memo_amount is not None
                               else line.amount)
        return max(ZERO, line.amount - personal_amount - f.limit_amount * line.nights)
    raise ValueError(f"unknown finding code {code!r}")


def _actions(codes: list[str], report_status: str) -> list[str]:
    paid = report_status == "PAID"
    wanted = set()
    for code in codes:
        if code == "MISSING_RECEIPT":
            wanted.add(REQUEST_RECEIPT if paid else HOLD)
        elif code == "MISCATEGORIZED":
            wanted.add(RECLASS)
        else:   # PERMANENT codes and DUPLICATE
            wanted.add(RECOVER if paid else REJECT)
    return [a for a in ACTION_ORDER if a in wanted]


def _approver(codes: list[str], out: Decimal, grade: str) -> str:
    if not codes:
        return NO_APPROVER
    if codes == ["MISSING_RECEIPT"]:
        return MANAGER
    if out >= config.MANAGER_LIMIT or FINANCE_CODES & set(codes) or grade == "DIRECTOR":
        return FINANCE
    return MANAGER


def decide(line: Line, findings: list[Finding], needs_human_reasons=(),
           client_meal: bool = False, attendee_names_quoted: bool = False,
           check_attendees: bool = False) -> Decision:
    """Turn verified findings into the line decision.

    client_meal / attendee_names_quoted: the line is a client meal (quote verified by the
    guards) and the memo lists attendee names (quote verified). Policy 4.3 sets the client
    limit, 4.4 requires the names; either gap -> NEEDS_HUMAN.

    check_attendees: policy 4.4, every group meal states the number of attendees. On a Meals
    line with the field blank, a (verified) finding must carry attendees_count, else
    NEEDS_HUMAN. Skipped when the line is MISCATEGORIZED (not really a meal). The model paths
    turn this on; the naive/rules baselines keep their own handling.
    """
    by_code: dict[str, Finding] = {}
    for f in findings:
        if f.code not in DIFFERENCE_TYPE:
            raise ValueError(f"unknown finding code {f.code!r}")
        by_code.setdefault(f.code, f)   # one finding per code; the first one wins

    reasons = list(needs_human_reasons)
    if client_meal:
        meal = by_code.get("MEAL_OVER_LIMIT")
        if meal is not None and CLIENT_CLAUSE not in meal.cites:
            reasons.append(f"client meal must use {CLIENT_CLAUSE}")
        if not attendee_names_quoted:
            reasons.append("4.4 attendee names missing")
    if (check_attendees and line.category == "Meals" and line.attendees_count is None
            and "MISCATEGORIZED" not in by_code
            and not any(f.attendees_count is not None for f in findings)):
        reasons.append(ATTENDEES_MISSING)
    dollars_by_code: dict[str, Decimal | None] = {}
    for code, f in by_code.items():
        if code == "AIRFARE_CLASS":
            # Policy 6.1: business class is allowed only when the flight is OVER the threshold.
            # Python compares; the model only quotes the threshold and the flight time.
            if f.flight_hours is None or f.limit_amount is None:
                reasons.append("flight time or hour threshold unknown for AIRFARE_CLASS")
            elif f.flight_hours > f.limit_amount:
                continue   # long enough flight: business class is in policy, not a finding
        value = _dollars(f, line, by_code)
        if value is not None:
            value = _cents(value)
        # A limit finding that is not actually over the limit is not a finding.
        if code in ("MEAL_OVER_LIMIT", "LODGING_OVER_LIMIT") and value == ZERO:
            continue
        if value is None:
            reasons.append(f"dollars for {code} cannot be computed")
        dollars_by_code[code] = value

    codes = list(dollars_by_code)
    notes = []
    for code in codes:
        cites = by_code[code].cites
        if code in LIMIT_CODES and cites and all(c.startswith("Appendix ") for c in cites):
            notes.append(f"{CITATION_INCOMPLETE}: {code} cites only {', '.join(cites)}")
    held =_cents(sum((line.amount for c in codes if DIFFERENCE_TYPE[c] == TIMING), ZERO))

    if reasons:
        status, out, approver, actions = NEEDS_HUMAN, None, None, []
    else:
        out = _cents(sum((v for c, v in dollars_by_code.items()
                          if DIFFERENCE_TYPE[c] != TIMING), ZERO))
        status = OK
        approver = _approver(codes, out, line.grade)
        actions = _actions(codes, line.report_status)

    return Decision(
        line_id=line.line_id,
        codes=codes,
        cites={c: list(by_code[c].cites) for c in codes},
        dollars_by_code=dollars_by_code,
        difference_types={c: DIFFERENCE_TYPE[c] for c in codes},
        dollars_out_of_policy=out,
        dollars_held=held,
        approver=approver,
        actions=actions,
        status=status,
        needs_human_reasons=reasons,
        notes=notes,
    )
