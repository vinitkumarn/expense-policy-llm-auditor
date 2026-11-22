import hashlib

from expaudit import config
from expaudit.policy import clauses_by_id, load_policy, policy_hash

DESIGN_IDS = ["3.1", "3.5", "4.1", "4.2", "4.3", "4.4", "4.5", "5.1", "6.1", "7.1", "8.1",
              "8.2", "Appendix B", "Appendix C"]


def test_parser_finds_key_clauses():
    clauses = clauses_by_id()
    for cid in ["4.2", "4.3", "4.5", "Appendix B", "Appendix C"]:
        assert cid in clauses
        assert clauses[cid].text
    assert clauses["4.2"].title == "Dinner"


def test_every_design_clause_present_plus_filler():
    clauses = load_policy()
    ids = [c.clause_id for c in clauses]
    assert len(ids) == len(set(ids))
    for cid in DESIGN_IDS:
        assert cid in ids
    filler = [i for i in ids if i not in DESIGN_IDS]
    assert len(filler) >= 8


def test_section_headers_are_not_clauses_and_text_stops_at_next_heading():
    clauses = clauses_by_id()
    assert "Meals" not in clauses["3.5"].text          # "# 4 Meals" ends clause 3.5
    assert not any(cid.startswith("Appendices") for cid in clauses)
    assert clauses["4.5"].text == (
        "Alcohol is not reimbursable, except as part of client entertainment under 4.3."
    )


def test_appendix_b_not_edited_for_c1():
    clauses = clauses_by_id()
    b, c = clauses["Appendix B"].text, clauses["Appendix C"].text
    assert "Tier 1 dinner, per person: $100" in b
    assert "$85" not in b
    assert "C-1" in c and "2026-10-01" in c and "$85 per person" in c


def test_policy_hash_is_sha256_of_file():
    expected = hashlib.sha256(config.POLICY_PATH.read_bytes()).hexdigest()
    assert policy_hash() == expected
    assert policy_hash() == policy_hash()
