from datetime import date
from decimal import Decimal as D

import pytest

from conftest import make_line
from expaudit import db, generator
from expaudit.decide import (DIFFERENCE_TYPE, HOLD, RECLASS, RECOVER, REJECT, REQUEST_RECEIPT,
                             Finding, decide)

MEAL = lambda limit, **kw: Finding("MEAL_OVER_LIMIT", ("4.2", "Appendix B"), limit_amount=D(limit), **kw)
LODGING = lambda limit: Finding("LODGING_OVER_LIMIT", ("5.1", "Appendix B"), limit_amount=D(limit))


def test_difference_types_match_the_design_table():
    assert DIFFERENCE_TYPE == generator.DIFFERENCE_TYPE


# --- citation incomplete: appendix-only limit finding is kept, only a note is added ----------
@pytest.mark.parametrize("code, rule, line_kw", [
    ("MEAL_OVER_LIMIT", "4.2", {"amount": D("340.00"), "attendees_count": 3}),
    ("LODGING_OVER_LIMIT", "5.1", {"amount": D("600.00"), "nights": 2}),
])
def test_limit_finding_citing_only_appendix_gets_note_decision_unchanged(code, rule, line_kw):
    line = make_line(**line_kw)
    full = decide(line, [Finding(code, (rule, "Appendix B"), limit_amount=D("85"))])
    only = decide(line, [Finding(code, ("Appendix B",), limit_amount=D("85"))])
    assert full.notes == []
    assert only.notes == [f"citation incomplete: {code} cites only Appendix B"]
    for attr in ("codes", "dollars_by_code", "dollars_out_of_policy", "dollars_held",
                 "approver", "actions", "status", "needs_human_reasons"):
        assert getattr(only, attr) == getattr(full, attr), attr
    assert only.status == "OK" and only.dollars_out_of_policy > 0
    assert only.to_dict()["notes"] == only.notes


# --- dollars per code -------------------------------------------------------------------------
def test_meal_over_limit_times_attendees():
    d = decide(make_line(amount=D("340.00"), attendees_count=3), [MEAL("85")])
    assert d.dollars_by_code == {"MEAL_OVER_LIMIT": D("85.00")}
    assert d.dollars_out_of_policy == D("85.00")
    assert d.difference_types == {"MEAL_OVER_LIMIT": "PERMANENT"}


def test_meal_attendees_from_finding_when_field_blank():
    d = decide(make_line(amount=D("680.00"), attendees_count=None), [MEAL("150", attendees_count=4)])
    assert d.dollars_out_of_policy == D("80.00")


def test_meal_attendees_unknown_needs_human():
    d = decide(make_line(amount=D("260.00"), attendees_count=None), [MEAL("100")])
    assert d.status == "NEEDS_HUMAN" and d.dollars_out_of_policy is None


def test_meal_alcohol_removed_first_m01():
    d = decide(make_line(amount=D("130.00")),
               [Finding("ALCOHOL", ("4.5",), memo_amount=D("30")), MEAL("70")])
    assert d.dollars_by_code == {"ALCOHOL": D("30.00"), "MEAL_OVER_LIMIT": D("30.00")}
    assert d.dollars_out_of_policy == D("60.00")
    assert d.approver == "FINANCE"


def test_limit_finding_not_over_limit_is_dropped():
    d = decide(make_line(amount=D("300.00"), attendees_count=3), [MEAL("100")])
    assert d.codes == [] and d.approver == "NONE" and d.actions == [] and d.status == "OK"
    assert d.dollars_out_of_policy == D("0.00")


def test_meal_floor_zero_when_alcohol_brings_it_under():
    d = decide(make_line(amount=D("68.00")),
               [Finding("ALCOHOL", ("4.5",), memo_amount=D("18")), MEAL("70")])
    assert d.codes == ["ALCOHOL"] and d.dollars_out_of_policy == D("18.00")


def test_lodging_minus_personal_items_times_nights_m02():
    line = make_line(category="Lodging", amount=D("680.00"), nights=2, city="New York")
    d = decide(line, [Finding("PERSONAL_ITEM", ("7.1",), memo_amount=D("40")), LODGING("300")])
    assert d.dollars_by_code == {"PERSONAL_ITEM": D("40.00"), "LODGING_OVER_LIMIT": D("40.00")}
    assert d.dollars_out_of_policy == D("80.00")


def test_personal_item_whole_line_when_no_amount():
    d = decide(make_line(category="Other", amount=D("120.00")), [Finding("PERSONAL_ITEM", ("7.1",))])
    assert d.dollars_out_of_policy == D("120.00") and d.approver == "FINANCE"


AIR = lambda hours, fare=None: Finding("AIRFARE_CLASS", ("6.1",), limit_amount=D("6"),
                                       memo_amount=None if fare is None else D(fare),
                                       flight_hours=None if hours is None else D(hours))


def test_airfare_class_is_amount_minus_economy_fare():
    d = decide(make_line(category="Airfare", amount=D("1480.00")), [AIR("5.58", "460")])
    assert d.dollars_out_of_policy == D("1020.00") and d.approver == "FINANCE"
    assert d.status == "OK"


def test_airfare_over_threshold_is_dropped_by_python():
    d = decide(make_line(category="Airfare", amount=D("2980.00")), [AIR("9.33", "900")])
    assert d.codes == [] and d.status == "OK" and d.approver == "NONE"


def test_airfare_exactly_six_hours_is_still_a_finding():
    d = decide(make_line(category="Airfare", amount=D("1000.00")), [AIR("6", "400")])
    assert d.codes == ["AIRFARE_CLASS"] and d.dollars_out_of_policy == D("600.00")


def test_airfare_flight_hours_unknown_needs_human():
    d = decide(make_line(category="Airfare", amount=D("1000.00")), [AIR(None, "400")])
    assert d.codes == ["AIRFARE_CLASS"] and d.status == "NEEDS_HUMAN"
    no_limit = Finding("AIRFARE_CLASS", ("6.1",), memo_amount=D("400"), flight_hours=D("5"))
    assert decide(make_line(category="Airfare", amount=D("1000.00")), [no_limit]).status == "NEEDS_HUMAN"


def test_airfare_without_economy_fare_needs_human_g05():
    d = decide(make_line(category="Airfare", amount=D("1210.00")), [AIR("4.17")])
    assert d.codes == ["AIRFARE_CLASS"]
    assert d.status == "NEEDS_HUMAN" and d.dollars_out_of_policy is None
    assert d.approver is None and d.actions == []


def test_alcohol_without_amount_needs_human():
    d = decide(make_line(), [Finding("ALCOHOL", ("4.5",))])
    assert d.status == "NEEDS_HUMAN" and d.dollars_out_of_policy is None


def test_duplicate_is_full_line_error():
    d = decide(make_line(category="Ground Transportation", amount=D("146.20")),
               [Finding("DUPLICATE", ("3.5",))])
    assert d.dollars_out_of_policy == D("146.20") and d.difference_types == {"DUPLICATE": "ERROR"}
    assert d.approver == "MANAGER" and d.actions == [REJECT]


def test_miscategorized_is_zero_and_reclass():
    d = decide(make_line(amount=D("189.00")), [Finding("MISCATEGORIZED", ("2.2",))])
    assert d.dollars_out_of_policy == D("0.00") and d.actions == [RECLASS]
    assert d.approver == "MANAGER" and d.difference_types == {"MISCATEGORIZED": "ERROR"}


def test_missing_receipt_is_held_not_out_of_policy():
    d = decide(make_line(category="Lodging", amount=D("412.00"), nights=3, receipt_attached=False),
               [Finding("MISSING_RECEIPT", ("3.1",))])
    assert d.dollars_out_of_policy == D("0.00") and d.dollars_held == D("412.00")
    assert d.difference_types == {"MISSING_RECEIPT": "TIMING"}
    assert d.approver == "MANAGER" and d.actions == [HOLD]


def test_unknown_code_rejected():
    with pytest.raises(ValueError):
        decide(make_line(), [Finding("MADE_UP")])


# --- M03: held never added to out of policy; two actions ---------------------------------------
def test_m03_held_and_out_kept_separate_with_two_actions():
    line = make_line(category="Lodging", amount=D("720.00"), nights=2, city="Seattle",
                     receipt_attached=False)
    d = decide(line, [Finding("MISSING_RECEIPT", ("3.1",)), LODGING("300")])
    assert d.dollars_out_of_policy == D("120.00")
    assert d.dollars_held == D("720.00")
    assert d.dollars_out_of_policy != d.dollars_out_of_policy + d.dollars_held
    assert d.actions == [REJECT, HOLD]
    assert d.approver == "MANAGER" and d.status == "OK"


# --- approver ----------------------------------------------------------------------------------
@pytest.mark.parametrize("out, approver", [("249.99", "MANAGER"), ("250.00", "FINANCE")])
def test_manager_limit_boundary(out, approver):
    amount = D("70.00") + D(out)   # Tier 2 dinner $70 for one person
    d = decide(make_line(amount=amount), [MEAL("70")])
    assert d.dollars_out_of_policy == D(out)
    assert d.approver == approver


def test_director_own_line_goes_to_finance_g04():
    d = decide(make_line(amount=D("90.00"), grade="DIRECTOR"), [MEAL("70")])
    assert d.dollars_out_of_policy == D("20.00") and d.approver == "FINANCE"


def test_no_findings_no_approver():
    d = decide(make_line(), [])
    assert d.approver == "NONE" and d.actions == [] and d.status == "OK"


# --- actions: SUBMITTED vs PAID ----------------------------------------------------------------
@pytest.mark.parametrize("status, permanent, receipt", [
    ("SUBMITTED", REJECT, HOLD),
    ("PAID", RECOVER, REQUEST_RECEIPT),
])
def test_actions_by_report_status(status, permanent, receipt):
    assert decide(make_line(amount=D("130.00"), report_status=status),
                  [MEAL("85")]).actions == [permanent]
    assert decide(make_line(amount=D("95.00"), report_status=status, receipt_attached=False),
                  [Finding("MISSING_RECEIPT", ("3.1",))]).actions == [receipt]
    assert decide(make_line(amount=D("88.40"), report_status=status),
                  [Finding("DUPLICATE", ("3.5",))]).actions == [permanent]
    assert decide(make_line(amount=D("189.00"), report_status=status),
                  [Finding("MISCATEGORIZED", ("2.2",))]).actions == [RECLASS]


# --- C-1: decide takes the limit from the finding ------------------------------------------------
def test_tier1_dinner_day_before_c1_uses_100():
    line = make_line(city="San Francisco", transaction_date=date(2026, 9, 30), amount=D("98.00"))
    d = decide(line, [MEAL("100")])
    assert d.codes == [] and d.approver == "NONE"


def test_tier1_dinner_on_c1_date_uses_85():
    line = make_line(city="San Francisco", transaction_date=date(2026, 10, 1), amount=D("90.00"))
    d = decide(line, [Finding("MEAL_OVER_LIMIT", ("4.2", "Appendix C"), limit_amount=D("85"))])
    assert d.dollars_out_of_policy == D("5.00")
    assert d.approver == "MANAGER" and d.actions == [REJECT]


# --- NEEDS_HUMAN: unknown dollars are null, never 0.00 ------------------------------------------
def test_needs_human_reason_blanks_dollars_approver_actions():
    d = decide(make_line(amount=D("95.00")), [MEAL("70")], ["client meal without names"])
    assert d.status == "NEEDS_HUMAN"
    assert d.dollars_out_of_policy is None and d.approver is None and d.actions == []


def test_g01_decision_is_null_dollars_needs_human(con):
    """G01 'drinks included', no amount: grey -> no dollars, no approver, no action.
    (The rules baseline does not catch 'drinks'; this tests decide() with the grey reason.)"""
    line = db.load_line(con, "G01")
    d = decide(line, [], ["alcohol mentioned but not itemized"])
    assert d.status == "NEEDS_HUMAN"
    assert d.dollars_out_of_policy is None
    assert d.approver is None and d.actions == []


# --- client meals: 4.3 limit, 4.4 names -------------------------------------------------------
CLIENT_MEAL = lambda limit="150": Finding("MEAL_OVER_LIMIT", ("4.3",), limit_amount=D(limit))


def test_g03_style_client_meal_on_dinner_limit_no_names_needs_human():
    line = make_line(amount=D("450.00"), attendees_count=3, city="New York")
    d = decide(line, [Finding("MEAL_OVER_LIMIT", ("4.2", "Appendix C"), limit_amount=D("85"))],
               client_meal=True)
    assert d.status == "NEEDS_HUMAN" and d.dollars_out_of_policy is None and d.approver is None
    assert "client meal must use 4.3" in d.needs_human_reasons
    assert "4.4 attendee names missing" in d.needs_human_reasons


def test_client_meal_no_names_needs_human_even_with_no_finding():
    d = decide(make_line(amount=D("450.00"), attendees_count=3), [], client_meal=True)
    assert d.status == "NEEDS_HUMAN" and d.needs_human_reasons == ["4.4 attendee names missing"]


def test_client_meal_citing_4_3_with_names_uses_the_4_3_limit():
    line = make_line(amount=D("700.00"), attendees_count=4, city="London")
    d = decide(line, [CLIENT_MEAL()], client_meal=True, attendee_names_quoted=True)
    assert d.status == "OK" and d.dollars_out_of_policy == D("100.00")
    assert d.cites == {"MEAL_OVER_LIMIT": ["4.3"]} and d.approver == "MANAGER"
    assert d.actions == [REJECT]


def test_client_meal_with_names_under_the_4_3_limit_is_clean():
    d = decide(make_line(amount=D("560.00"), attendees_count=4), [CLIENT_MEAL()],
               client_meal=True, attendee_names_quoted=True)
    assert d.status == "OK" and d.codes == [] and d.dollars_out_of_policy == D("0.00")


def test_client_meal_citing_4_3_but_no_names_needs_human():
    d = decide(make_line(amount=D("700.00"), attendees_count=4), [CLIENT_MEAL()],
               client_meal=True)
    assert d.status == "NEEDS_HUMAN" and d.needs_human_reasons == ["4.4 attendee names missing"]


def test_normal_dinner_unaffected_by_the_client_rule():
    d = decide(make_line(amount=D("340.00"), attendees_count=3), [MEAL("85")])
    assert d.status == "OK" and d.dollars_out_of_policy == D("85.00")


# --- 4.4 attendee count: Python decides when the field is blank (model paths) ----------------
def test_g02_style_blank_attendees_no_findings_needs_human():
    line = make_line(amount=D("260.00"), attendees_count=None, city="San Francisco",
                     memo="Group dinner with the implementation team.")
    d = decide(line, [], check_attendees=True)
    assert d.status == "NEEDS_HUMAN" and d.needs_human_reasons == ["attendee count missing (4.4)"]
    assert d.dollars_out_of_policy is None and d.approver is None and d.actions == []


def test_w05_style_blank_field_verified_attendees_from_memo_unchanged():
    line = make_line(amount=D("680.00"), attendees_count=None, city="Dallas")
    d = decide(line, [Finding("MEAL_OVER_LIMIT", ("4.3",), limit_amount=D("150"),
                              attendees_count=4)],
               client_meal=True, attendee_names_quoted=True, check_attendees=True)
    assert d.status == "OK" and d.dollars_out_of_policy == D("80.00")
    assert d.approver == "MANAGER" and d.actions == [REJECT]


def test_one_person_dinner_with_field_filled_unchanged():
    d = decide(make_line(amount=D("50.00"), attendees_count=1), [], check_attendees=True)
    assert d.status == "OK" and d.codes == [] and d.approver == "NONE"


def test_a01_style_miscategorized_meals_line_not_held_for_attendees():
    line = make_line(merchant="Hilton Garden Inn", amount=D("189.00"), attendees_count=None,
                     memo="hotel 1 night")
    d = decide(line, [Finding("MISCATEGORIZED", ("2.2",))], check_attendees=True)
    assert d.status == "OK" and d.actions == [RECLASS]


def test_attendee_check_off_by_default_baselines_unchanged():
    d = decide(make_line(amount=D("260.00"), attendees_count=None), [])
    assert d.status == "OK" and d.needs_human_reasons == []
