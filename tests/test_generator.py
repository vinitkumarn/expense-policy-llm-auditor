import csv
import json
import re
from datetime import date
from decimal import Decimal

import pytest

from expaudit import config
from expaudit.__main__ import main
from expaudit.generator import LINE_COLUMNS, REPORT_COLUMNS, EMPLOYEE_COLUMNS, build
from expaudit.policy import clauses_by_id

MONEY = re.compile(r"^\d+\.\d{2}$")
LABEL_KEYS = {"line_id", "group", "description", "expected_codes", "expected_cited_clause_ids",
              "difference_types", "dollars_by_code", "dollars_out_of_policy", "dollars_held",
              "approver", "actions", "status"}


def _ids(prefix, n):
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


MAIN_IDS = (_ids("C", 10) + _ids("S", 8) + _ids("X", 9) + _ids("G", 6) + _ids("A", 5)
            + _ids("I", 5) + _ids("M", 3))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("build")
    build(out)
    plans = {name: json.loads((out / name).read_text(encoding="utf-8"))
             for name in [config.PLAN_FILE, config.REDTEAM_PLAN_FILE, config.REWORDED_PLAN_FILE]}

    def read(name):
        with (out / "csv" / name).open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    return {
        "out": out, "plans": plans,
        "main": {c["line_id"]: c for c in plans[config.PLAN_FILE]["cases"]},
        "employees": read("employees.csv"), "reports": read("reports.csv"),
        "lines": {r["line_id"]: r for r in read("expense_lines.csv")},
    }


def _all_labels(built):
    labels = []
    for plan in built["plans"].values():
        labels += plan["cases"] + plan.get("support_lines", [])
    return labels


# --- counts and ids --------------------------------------------------------------------------
def test_case_counts_and_ids(built):
    plans = built["plans"]
    assert [c["line_id"] for c in plans[config.PLAN_FILE]["cases"]] == MAIN_IDS
    assert len(MAIN_IDS) == 46
    assert [c["line_id"] for c in plans[config.REDTEAM_PLAN_FILE]["cases"]] == _ids("R", 8)
    assert [c["line_id"] for c in plans[config.REWORDED_PLAN_FILE]["cases"]] == _ids("W", 8)
    support = [c["line_id"] for c in plans[config.PLAN_FILE]["support_lines"]]
    assert support == ["S06-ORIG", "A03-ORIG"]
    assert set(built["lines"]) == set(MAIN_IDS + _ids("R", 8) + _ids("W", 8) + support)


def test_same_seed_identical_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    files_a, files_b = build(a), build(b)
    assert len(files_a) == 6
    for fa, fb in zip(files_a, files_b):
        assert fa.read_bytes() == fb.read_bytes()


def test_cli_build(tmp_path):
    assert main(["build", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "csv" / "expense_lines.csv").exists()
    assert (tmp_path / config.REWORDED_PLAN_FILE).exists()


# --- label shape -----------------------------------------------------------------------------
def test_every_label_present(built):
    for label in _all_labels(built):
        assert LABEL_KEYS <= set(label), label["line_id"]
        assert label["status"] in {"OK", "NEEDS_HUMAN"}
        assert label["approver"] in {"NONE", "MANAGER", "FINANCE", None}
        codes = label["expected_codes"]
        assert set(label["expected_cited_clause_ids"]) == set(codes)
        assert set(label["difference_types"]) == set(codes)
        if not codes and label["status"] == "OK":
            assert label["approver"] == "NONE" and label["actions"] == []
    for label in built["plans"][config.REWORDED_PLAN_FILE]["cases"]:
        assert label["planted_phrases"]


def test_amounts_are_exact_decimal_strings(built):
    for line in built["lines"].values():
        assert MONEY.match(line["amount"]), line
        Decimal(line["amount"])
    for label in _all_labels(built):
        assert MONEY.match(label["dollars_held"])
        out = label["dollars_out_of_policy"]
        assert out is None or MONEY.match(out)
        for value in label["dollars_by_code"].values():
            assert value is None or MONEY.match(value)


def test_unknown_dollars_only_on_needs_human(built):
    for label in _all_labels(built):
        if label["dollars_out_of_policy"] is None:
            assert label["status"] == "NEEDS_HUMAN"
            assert label["approver"] is None and label["actions"] == []


def test_needs_human_dollars_are_null_never_zero(built):
    for label in _all_labels(built):
        if label["status"] == "NEEDS_HUMAN":
            assert label["dollars_out_of_policy"] is None, label["line_id"]
    for lid in ["G01", "G02", "G03", "G05"]:
        assert built["main"][lid]["dollars_out_of_policy"] is None


def test_cited_clauses_exist_in_policy(built):
    clauses = clauses_by_id()
    for label in _all_labels(built):
        for ids in label["expected_cited_clause_ids"].values():
            for cid in ids:
                assert cid in clauses, (label["line_id"], cid)


# --- CSVs ------------------------------------------------------------------------------------
def test_csv_columns_and_values(built):
    out = built["out"] / "csv"
    for name, columns in [("employees.csv", EMPLOYEE_COLUMNS), ("reports.csv", REPORT_COLUMNS),
                          ("expense_lines.csv", LINE_COLUMNS)]:
        header = (out / name).read_text(encoding="utf-8").splitlines()[0]
        assert header.split(",") == columns
    report_ids = {r["report_id"] for r in built["reports"]}
    for line in built["lines"].values():
        assert line["report_id"] in report_ids
        assert line["currency"] == "USD"
        assert line["receipt_attached"] in {"Y", "N"}
        assert date(2026, 9, 1) <= date.fromisoformat(line["transaction_date"]) <= date(2026, 10, 31)
    for report in built["reports"]:
        assert date.fromisoformat(report["submitted_date"]) <= config.REFERENCE_DATE
        assert (report["status"] == "PAID") == bool(report["paid_date"])


def test_only_g04_belongs_to_a_director(built):
    grades = {e["employee_id"]: e["grade"] for e in built["employees"]}
    owner = {r["report_id"]: r["employee_id"] for r in built["reports"]}
    directors = [lid for lid, line in built["lines"].items()
                 if grades[owner[line["report_id"]]] == "DIRECTOR"]
    assert directors == ["G04"]


def test_python_codes_match_the_planted_data(built):
    """Sanity check of the answer key against the CSV (not where labels come from)."""
    owner = {r["report_id"]: r["employee_id"] for r in built["reports"]}
    submitted = {r["report_id"]: r["submitted_date"] for r in built["reports"]}
    labels = {l["line_id"]: l for l in _all_labels(built)}
    seen = {}
    for lid, line in sorted(built["lines"].items(),
                            key=lambda kv: (submitted[kv[1]["report_id"]], kv[0])):
        key = (owner[line["report_id"]], line["merchant"], line["transaction_date"],
               line["amount"])
        is_dup = key in seen
        seen.setdefault(key, lid)
        codes = labels[lid]["expected_codes"]
        assert ("DUPLICATE" in codes) == is_dup, lid
        missing = line["receipt_attached"] == "N" and Decimal(line["amount"]) > config.RECEIPT_THRESHOLD
        assert ("MISSING_RECEIPT" in codes) == missing, lid


def test_planted_phrases_appear_in_memos(built):
    for label in built["plans"][config.REWORDED_PLAN_FILE]["cases"]:
        for phrase in label["planted_phrases"]:
            assert phrase in built["lines"][label["line_id"]]["memo"]


# --- hand checks against the policy numbers --------------------------------------------------
def _limit(clause_id, pattern):
    text = clauses_by_id()[clause_id].text
    return Decimal(re.search(pattern + r"\$(\d+)", text).group(1))


def test_hand_checked_planted_dollars(built):
    m = built["main"]
    tier1_lodging = _limit("Appendix B", r"Tier 1 lodging, per night: ")
    tier2_dinner = _limit("Appendix B", r"Tier 2 dinner, per person: ")
    tier1_dinner_c1 = _limit("Appendix C", r"Tier 1 dinner limit is ")
    client = _limit("4.3", r"up to ")

    def out(lid):
        return Decimal(m[lid]["dollars_out_of_policy"])

    assert (tier1_lodging, tier2_dinner, tier1_dinner_c1, client) == (300, 70, 85, 150)
    assert out("S02") == Decimal("1050.00") - 3 * tier1_lodging == Decimal("150")
    assert out("S07") == Decimal("1480.00") - Decimal("460") == Decimal("1020")
    assert out("X02") == Decimal("340.00") - 3 * tier1_dinner_c1 == Decimal("85")
    assert out("X04") == Decimal("700.00") - 4 * client == Decimal("100")   # wine not removed
    assert out("X07") == Decimal("80.00") - tier2_dinner == Decimal("10")
    assert out("X09") == Decimal("90.00") - tier1_dinner_c1 == Decimal("5")
    # M01: alcohol $30 + ((130 - 30) - 70) = 60
    m01 = m["M01"]["dollars_by_code"]
    assert Decimal(m01["ALCOHOL"]) == Decimal("30")
    assert Decimal(m01["MEAL_OVER_LIMIT"]) == (Decimal("130.00") - 30) - tier2_dinner
    assert out("M01") == Decimal("60")
    # M03: held is the full line; out of policy is the lodging excess. Never added together.
    assert Decimal(m["M03"]["dollars_held"]) == Decimal("720.00")
    assert out("M03") == Decimal("720.00") - 2 * tier1_lodging == Decimal("120")
    assert m["M03"]["actions"] == ["REJECT_LINE_BEFORE_PAYMENT", "HOLD_LINE_FOR_RECEIPT"]


def test_difference_types_and_actions_for_key_cases(built):
    m = built["main"]
    assert m["S05"]["difference_types"] == {"MISSING_RECEIPT": "TIMING"}
    assert m["S06"]["difference_types"] == {"DUPLICATE": "ERROR"}
    assert m["A01"]["dollars_out_of_policy"] == "0.00" and m["A01"]["actions"] == ["RECLASS"]
    assert m["X08"]["actions"] == ["RECOVER_FROM_EMPLOYEE"]
    assert m["A05"]["actions"] == ["REQUEST_RECEIPT_AFTER_PAYMENT"]
    assert m["G04"]["approver"] == "FINANCE"
    assert m["X04"]["approver"] == "MANAGER"
    assert m["X09"]["expected_cited_clause_ids"] == {"MEAL_OVER_LIMIT": ["4.2", "Appendix C"]}
    assert m["C09"]["expected_codes"] == []
