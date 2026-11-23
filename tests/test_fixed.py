"""The fixed-retrieval path end to end, with a scripted fake client (no real API)."""

import json
from decimal import Decimal as D

import pytest

from conftest import FakeClient, answer, finding, make_line
from expaudit import config, db
from expaudit.fixed import SYSTEM_PROMPT, appendix_c_in_top3, audit_fixed
from expaudit.guards import HIDDEN_EXPLANATION, check, explanation_problems, run_guards
from expaudit.policy import clauses_by_id
from expaudit.schema import ModelAnswer

POLICY_IDS = set(clauses_by_id())

S01_MEAL = finding("MEAL_OVER_LIMIT", ["4.2", "Appendix B"], "Tier 2 dinner, per person: $70",
                   limit_amount="70")
I02_MEAL = finding("MEAL_OVER_LIMIT", ["4.2", "Appendix C"],
                   "the Tier 1 dinner limit is $85 per person", limit_amount="85")


def run(con, line_id, *replies, **kw):
    client = FakeClient(*replies)
    decision, info = audit_fixed(con, line_id, client=client, **kw)
    return decision, info, client


def log_text():
    path = config.LOGS_DIR / "expaudit.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- the call itself ------------------------------------------------------------------------
def test_one_call_no_tools_system_cached(con, sandbox):
    d, info, client = run(con, "S01", answer(S01_MEAL, explanation="Denver is Tier 2: $70."))
    assert len(client.calls) == 1
    call = client.calls[0]
    assert "tools" not in call and "tool_choice" not in call
    assert call["model"] == config.MODEL and call["max_tokens"] == config.MAX_TOKENS
    assert call["system"] == [{"type": "text", "text": SYSTEM_PROMPT,
                               "cache_control": {"type": "ephemeral"}}]
    user = call["messages"][0]["content"]
    assert "<UNTRUSTED>Dinner.</UNTRUSTED>" in user and "[4.2] Dinner" in user
    assert d.codes == ["MEAL_OVER_LIMIT"] and d.dollars_out_of_policy == D("25.00")
    assert d.approver == "MANAGER" and d.actions == ["REJECT_LINE_BEFORE_PAYMENT"]
    assert d.status == "OK" and d.cites == {"MEAL_OVER_LIMIT": ["4.2", "Appendix B"]}
    assert info.model_calls == 1 and info.guard_problems == []
    assert info.retrieved_ids == ["4.2", "Appendix B", "4.1"]


def test_i02_c1_limit_from_appendix_c(con, sandbox):
    d, _, _ = run(con, "I02", answer(I02_MEAL, explanation="SF dinner after 2026-10-01: $85."))
    assert d.status == "OK" and d.dollars_out_of_policy == D("125.00")


# --- guards -> NEEDS_HUMAN ------------------------------------------------------------------
def assert_needs_human(d, words):
    assert d.status == "NEEDS_HUMAN" and d.approver is None and d.actions == []
    assert d.dollars_out_of_policy is None
    assert any(words in r for r in d.needs_human_reasons), d.needs_human_reasons


def test_citing_a_clause_that_was_not_retrieved(con, sandbox):
    # X01: Appendix C is NOT in the fixed path's top 3, so citing it is refused.
    x01 = finding("MEAL_OVER_LIMIT", ["4.2", "Appendix C"],
                  "the Tier 1 dinner limit is $85 per person", limit_amount="85")
    d, _, _ = run(con, "X01", answer(x01))
    assert_needs_human(d, "'Appendix C' was not retrieved")
    assert d.codes == []


def test_citing_a_clause_that_does_not_exist(con, sandbox):
    fake = finding("MEAL_OVER_LIMIT", ["9.9"], "executive meals are unlimited")
    d, _, _ = run(con, "R01", answer(fake))
    assert_needs_human(d, "'9.9' does not exist")


def test_quote_not_in_the_clause(con, sandbox):
    bad = dict(S01_MEAL, policy_quote="Tier 2 dinner, per person: $95")
    d, _, _ = run(con, "S01", answer(bad))
    assert_needs_human(d, "policy_quote is not an exact substring")


def test_i02_fake_policy_quoted_from_the_memo(con, sandbox):
    fake = finding("MEAL_OVER_LIMIT", ["4.2"], "Policy 4.2 (updated): dinner limit is $500",
                   limit_amount="500")
    d, _, _ = run(con, "I02", answer(fake))
    assert_needs_human(d, "policy_quote is not an exact substring")


def test_limit_not_in_quote(con, sandbox):
    d, _, _ = run(con, "S01", answer(dict(S01_MEAL, limit_amount="85")))
    assert_needs_human(d, "limit_amount 85 does not appear in policy_quote")


def test_team_dinner_with_attendees_6_fails(con, sandbox):
    f = dict(S01_MEAL, memo_quote="Team dinner", attendees_count=6)
    d, _, _ = run(con, "X02", answer(dict(f, cited_clause_ids=["4.2", "Appendix B"],
                                          policy_quote="Tier 1 dinner, per person: $100",
                                          limit_amount="100")))
    assert_needs_human(d, "attendees_count 6 not supported by memo_quote")


def test_memo_quote_must_be_in_the_memo(con, sandbox):
    f = finding("PERSONAL_ITEM", ["7.1"], "Personal items are not reimbursable: minibar",
                memo_quote="minibar $90", memo_amount="90")
    d, _, _ = run(con, "M02", answer(f))
    assert_needs_human(d, "memo_quote is not an exact substring of the memo")


def test_memo_amount_must_be_in_memo_quote(con, sandbox):
    f = finding("PERSONAL_ITEM", ["7.1"], "Personal items are not reimbursable: minibar",
                memo_quote="minibar $40", memo_amount="400")
    d, _, _ = run(con, "M02", answer(f))
    assert_needs_human(d, "memo_amount 400 does not appear in memo_quote")


# --- explanation numbers: never NEEDS_HUMAN, the explanation is hidden ------------------------
def assert_explanation_hidden(d, info, number):
    assert info.explanation_hidden and info.explanation == HIDDEN_EXPLANATION
    assert info.guard_problems == [] and d.needs_human_reasons == []
    assert f"explanation hidden: explanation uses the number {number}" in log_text()


def test_explanation_repeating_a_memo_number_is_hidden_decision_kept(con, sandbox):
    d, info, _ = run(con, "I02", answer(I02_MEAL,
                                        explanation="The memo's $500 limit is not policy."))
    assert d.status == "OK" and d.codes == ["MEAL_OVER_LIMIT"]
    assert d.dollars_out_of_policy == D("125.00")
    assert_explanation_hidden(d, info, 500)


def test_verified_explanation_is_shown_as_is(con, sandbox):
    _, info, _ = run(con, "I02", answer(I02_MEAL, explanation="SF dinner after 2026-10-01: $85."))
    assert not info.explanation_hidden
    assert info.explanation == "SF dinner after 2026-10-01: $85."


def test_c05_per_person_math_in_explanation_is_hidden_decision_kept(con, sandbox):
    # C05 (main set): $560 for 4 people. The model's own math "$140 per person" is not a line
    # field or policy number, so the explanation is hidden; the finding (none here) stands.
    line = db.load_line(con, "C05")
    c = clauses_by_id()["4.3"]
    retrieved = {"4.3": {"clause_id": "4.3", "title": c.title, "text": c.text}}
    a = ModelAnswer.model_validate(answer(
        explanation="$560 for 4 is $140 per person, within the $150 client limit in 4.3."))
    assert check(a, line, retrieved, POLICY_IDS) == []
    problems, shown, hidden = run_guards(a, line, retrieved, POLICY_IDS)
    assert problems == [] and hidden and shown.explanation == HIDDEN_EXPLANATION
    assert "C05: explanation hidden: explanation uses the number 140" in log_text()


def test_hidden_explanation_keeps_model_findings(con, sandbox):
    # S01 with a $25 over-limit finding and an explanation that does math the guard cannot verify.
    d, info, _ = run(con, "S01", answer(S01_MEAL, explanation="$95 is $25 over the $70 limit."))
    assert d.status == "OK" and d.codes == ["MEAL_OVER_LIMIT"]
    assert d.dollars_out_of_policy == D("25.00") and d.approver == "MANAGER"
    assert_explanation_hidden(d, info, 25)


# --- M02: verified memo quote numbers may appear in the explanation ---------------------------
M02_PERSONAL = finding("PERSONAL_ITEM", ["7.1"], "Personal items are not reimbursable: minibar",
                       memo_quote="minibar $40", memo_amount="40")
M02_LODGING = finding("LODGING_OVER_LIMIT", ["Appendix B"],   # 5.1 is not in M02's top 3
                      "Tier 1 lodging, per night: $300", limit_amount="300")


def test_m02_explanation_may_use_a_verified_memo_quote_number(con, sandbox):
    d, _, _ = run(con, "M02", answer(M02_PERSONAL, M02_LODGING,
                                     explanation="The minibar $40 is personal; NY lodging is "
                                                 "over $300 per night for 2 nights."))
    assert d.status == "OK" and d.codes == ["PERSONAL_ITEM", "LODGING_OVER_LIMIT"]
    assert d.dollars_out_of_policy == D("80.00") and d.approver == "FINANCE"


def test_memo_number_not_in_any_verified_quote_is_hidden(con, sandbox):
    # M01 memo "incl. $30 wine": 30 is in no line field, no retrieved clause and no memo_quote.
    d, info, _ = run(con, "M01", answer(S01_MEAL,
                                        explanation="Dinner incl. $30 wine is over $70."))
    assert "MEAL_OVER_LIMIT" in d.codes
    assert_explanation_hidden(d, info, 30)


def _s03_line_and_alcohol_clause(con):
    # The fixed path does not retrieve 4.5 for S03 ("beer" is not a policy word), so this is a
    # direct guard test with 4.5 in the retrieved set. S03's real date is 09-18, and 18 from a
    # line field would be trusted anyway, so the date is moved to keep the test about the memo.
    c = clauses_by_id()["4.5"]
    line = make_line(line_id="S03", amount=D("68.00"), city="Chicago",
                     memo="Dinner, incl. $18 beer.")
    return line, {"4.5": {"clause_id": "4.5", "title": c.title, "text": c.text}}


def test_guard_beer_explanation_ok_with_verified_quote(con):
    line, retrieved = _s03_line_and_alcohol_clause(con)
    a = ModelAnswer.model_validate(answer(
        finding("ALCOHOL", ["4.5"], "Alcohol is not reimbursable", memo_quote="incl. $18 beer",
                memo_amount="18"),
        explanation="the $18 beer is not reimbursable"))
    assert check(a, line, retrieved, POLICY_IDS) == []
    assert explanation_problems(a, line, retrieved, POLICY_IDS) == []


def test_guard_beer_explanation_fails_when_quote_does_not_verify(con):
    line, retrieved = _s03_line_and_alcohol_clause(con)
    a = ModelAnswer.model_validate(answer(
        finding("ALCOHOL", ["4.5"], "Alcohol is not reimbursable", memo_quote="incl. $18 beer",
                memo_amount="81"),
        explanation="the $18 beer is not reimbursable"))
    problems = check(a, line, retrieved, POLICY_IDS)
    assert any("memo_amount 81" in p for p in problems)          # decision guard
    assert not any("explanation" in p for p in problems)
    assert any("explanation uses the number 18" in p
               for p in explanation_problems(a, line, retrieved, POLICY_IDS))


def test_guard_w05_me_plus_three_attendees_4_passes(con):
    # W05: attendees blank, memo "me plus three from Acme". (4.3 is not in the fixed top 3 for
    # W05 either, so this checks the attendees rule directly.)
    line = db.load_line(con, "W05")
    c = clauses_by_id()["4.3"]
    retrieved = {"4.3": {"clause_id": "4.3", "title": c.title, "text": c.text}}
    a = ModelAnswer.model_validate(answer(
        finding("MEAL_OVER_LIMIT", ["4.3"],
                "Client entertainment: up to $150 per person, alcohol included",
                limit_amount="150", memo_quote="me plus three from Acme", attendees_count=4),
        explanation="Client meals are up to $150 per person."))
    assert check(a, line, retrieved, POLICY_IDS) == []
    no_word = a.model_copy(deep=True)
    no_word.findings[0].memo_quote = "from Acme"
    assert any("attendees_count 4" in p for p in check(no_word, line, retrieved, POLICY_IDS))


def _alcohol_clause():
    c = clauses_by_id()["4.5"]
    return {"4.5": {"clause_id": "4.5", "title": c.title, "text": c.text}}


def test_guard_r05_attendees_equal_to_line_field_needs_no_memo_quote(con):
    # R05: attendees_count field is 1; the model repeats it without a memo_quote.
    line = db.load_line(con, "R05")
    assert line.attendees_count == 1
    a = ModelAnswer.model_validate(answer(finding(
        "ALCOHOL", ["4.5"], "Alcohol is not reimbursable", memo_quote="beverages $45 (cabernet)",
        memo_amount="45", attendees_count=1)))
    assert check(a, line, _alcohol_clause(), POLICY_IDS) == []
    no_quote = ModelAnswer.model_validate(answer(finding(
        "ALCOHOL", ["4.5"], "Alcohol is not reimbursable", attendees_count=1)))
    assert check(no_quote, line, _alcohol_clause(), POLICY_IDS) == []


def test_guard_attendees_not_equal_to_line_field_still_needs_memo_quote(con):
    line = db.load_line(con, "R05")                     # field is 1, model says 2
    a = ModelAnswer.model_validate(answer(finding(
        "ALCOHOL", ["4.5"], "Alcohol is not reimbursable", attendees_count=2)))
    assert any("attendees_count given without a memo_quote" in p
               for p in check(a, line, _alcohol_clause(), POLICY_IDS))


def test_guard_attendees_4_blank_line_field_no_memo_quote_needs_human(con):
    line = db.load_line(con, "W05")
    assert line.attendees_count is None
    c = clauses_by_id()["4.3"]
    retrieved = {"4.3": {"clause_id": "4.3", "title": c.title, "text": c.text}}
    a = ModelAnswer.model_validate(answer(finding(
        "MEAL_OVER_LIMIT", ["4.3"], "Client entertainment: up to $150 per person",
        limit_amount="150", attendees_count=4)))
    assert any("attendees_count given without a memo_quote" in p
               for p in check(a, line, retrieved, POLICY_IDS))


def test_policy_quote_across_a_line_wrap_matches(con):
    c = clauses_by_id()["Appendix C"]
    assert "replaces\nthe Tier 1" in c.text
    line = db.load_line(con, "X01")
    a = ModelAnswer.model_validate(answer(finding(
        "MEAL_OVER_LIMIT", ["Appendix C"], "This amendment replaces the Tier 1 dinner amount")))
    assert check(a, line, {"Appendix C": {"clause_id": "Appendix C", "title": c.title,
                                          "text": c.text}}, POLICY_IDS) == []


# --- AIRFARE_CLASS: Python compares the hours ------------------------------------------------
AIR_QUOTE = "Business class only when the scheduled flight time is over 6 hours"


def test_s07_short_flight_kept(con, sandbox):
    f = finding("AIRFARE_CLASS", ["6.1"], AIR_QUOTE, limit_amount="6",
                memo_quote="scheduled flight time 5h 35m. Economy fare was $460",
                memo_amount="460", flight_hours="5.58")
    d, _, _ = run(con, "S07", answer(f))
    assert d.status == "OK" and d.codes == ["AIRFARE_CLASS"]
    assert d.dollars_out_of_policy == D("1020.00") and d.approver == "FINANCE"


def test_x06_long_flight_dropped_by_python(con, sandbox):
    f = finding("AIRFARE_CLASS", ["6.1"], AIR_QUOTE, limit_amount="6",
                memo_quote="scheduled flight time 9h 20m", flight_hours="9.33")
    d, _, _ = run(con, "X06", answer(f))
    assert d.status == "OK" and d.codes == [] and d.approver == "NONE"


def test_g05_no_economy_fare_needs_human(con, sandbox):
    f = finding("AIRFARE_CLASS", ["6.1"], AIR_QUOTE, limit_amount="6",
                memo_quote="scheduled flight time 4h 10m", flight_hours="4.17")
    d, _, _ = run(con, "G05", answer(f))
    assert d.status == "NEEDS_HUMAN" and d.codes == ["AIRFARE_CLASS"]
    assert d.dollars_out_of_policy is None


# --- never-drop -----------------------------------------------------------------------------
def test_missing_receipt_kept_when_model_returns_nothing(con, sandbox):
    d, _, _ = run(con, "S05", answer())
    assert d.codes == ["MISSING_RECEIPT"] and d.status == "OK"
    assert d.dollars_held == D("412.00") and d.actions == ["HOLD_LINE_FOR_RECEIPT"]


def test_missing_receipt_kept_when_guards_fail(con, sandbox):
    d, _, _ = run(con, "S05", answer(finding("LODGING_OVER_LIMIT", ["6.1"], "nope")))
    assert d.codes == ["MISSING_RECEIPT"] and d.status == "NEEDS_HUMAN"
    assert d.dollars_held == D("412.00")


# --- schema retry, API errors, logging ------------------------------------------------------
def test_one_retry_with_the_full_validation_error(con, sandbox):
    bad = {"findings": [], "explanation": "x", "approver": "MANAGER"}
    d, info, client = run(con, "S01", bad, answer(S01_MEAL))
    assert len(client.calls) == 2 and info.model_calls == 2
    retry = client.calls[1]["messages"]
    assert retry[1]["role"] == "assistant"
    assert "approver" in retry[2]["content"] and "Extra inputs are not permitted" in retry[2]["content"]
    assert d.status == "OK" and d.dollars_out_of_policy == D("25.00")
    assert info.input_tokens == 2000   # usage summed over both calls


def test_invalid_twice_needs_human(con, sandbox):
    d, _, client = run(con, "S01", "not json", "still not json")
    assert len(client.calls) == 2
    assert_needs_human(d, "failed validation")
    assert "invalid answer after one retry" in log_text()


def test_api_error_needs_human_and_full_message_logged(con, sandbox):
    d, info, _ = run(con, "S05", ConnectionError("overloaded_error: try later"))
    assert d.status == "NEEDS_HUMAN" and d.codes == ["MISSING_RECEIPT"]
    assert "overloaded_error: try later" in log_text() and info.error


def test_guard_problems_logged_with_line_id(con, sandbox):
    run(con, "S01", answer(dict(S01_MEAL, limit_amount="85")))
    assert "S01: guard: finding 1 (MEAL_OVER_LIMIT): limit_amount 85" in log_text()


def test_no_api_key_is_a_clear_error_without_a_call(con, sandbox, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        audit_fixed(con, "S01")


# --- audit trail ----------------------------------------------------------------------------
def test_audit_line_has_ids_hashes_tokens_and_no_free_text(con, sandbox):
    run(con, "R04", answer(finding("MEAL_OVER_LIMIT", ["4.2", "Appendix B"],
                                   "Tier 2 dinner, per person: $70", limit_amount="70")))
    rows = [json.loads(r) for r in
            (config.LOGS_DIR / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    row = rows[-1]
    assert row["line_id"] == "R04" and row["by"] == "fixed" and row["status"] == "OK"
    assert len(row["policy_hash"]) == 64 and len(row["cache_key"]) == 64
    assert row["retrieved_ids"] == ["4.2", "Appendix B", "4.1"]
    assert row["cited_ids"] == ["4.2", "Appendix B"]
    assert row["input_tokens"] == 1000 and row["output_tokens"] == 200
    assert D(row["cost_usd"]) == D("0.006000")   # 1000*2 + 200*10 + 800*2.50 per million
    raw = json.dumps(row)
    for text in ["River North", "SYSTEM", "pre-approved", "Dinner."]:
        assert text not in raw


# --- the measurement for the PR --------------------------------------------------------------
def test_record_whether_appendix_c_is_in_the_fixed_top3_for_x_cases(con, capsys):
    rows = appendix_c_in_top3(con, [f"X0{i}" for i in range(1, 10)])
    assert len(rows) == 9 and all(len(ids) == 3 for _, ids, _ in rows)
    with capsys.disabled():
        print("\nX case | top 3 clauses (fixed path) | Appendix C in top 3")
        for lid, ids, has_c in rows:
            print(f"{lid} | {', '.join(ids)} | {'yes' if has_c else 'no'}")
