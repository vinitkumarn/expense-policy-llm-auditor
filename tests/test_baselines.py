import json
from decimal import Decimal

from expaudit import config, db
from expaudit.baselines import PATTERNS, audit, naive, rules


def test_reworded_planted_phrases_match_no_rules_keyword_or_regex(data_dir):
    plan = json.loads((data_dir / config.REWORDED_PLAN_FILE).read_text(encoding="utf-8"))
    phrases = [p for case in plan["cases"] for p in case["planted_phrases"]]
    assert len(phrases) == 8
    for phrase in phrases:
        for pattern in PATTERNS:
            assert not pattern.search(phrase), (phrase, pattern.pattern)


def test_rules_limits_table_is_pre_c1():
    """Known gap, on purpose: the hardcoded table has the Appendix B $100, not C-1's $85."""
    assert config.RULES_LIMITS[("dinner", 1)] == Decimal("100")


def test_rules_misses_c1_on_x09(con):
    d = audit(con, "X09", "rules")   # SF dinner 10-01 $90: real limit $85, rules think $100
    assert d.codes == []


def test_naive_is_checks_only(con):
    for lid in ["S01", "S03", "M01"]:
        findings, reasons = naive(con, db.load_line(con, lid))
        assert findings == [] and reasons == []
    assert audit(con, "S05", "naive").codes == ["MISSING_RECEIPT"]
    assert audit(con, "S06", "naive").codes == ["DUPLICATE"]


def test_rules_keeps_python_checks_never_drop(con):
    d = audit(con, "M03", "rules")
    assert d.codes == ["MISSING_RECEIPT", "LODGING_OVER_LIMIT"]
    assert d.dollars_out_of_policy == Decimal("120.00") and d.dollars_held == Decimal("720.00")


def test_rules_examples(con):
    s03 = audit(con, "S03", "rules")
    assert s03.codes == ["ALCOHOL"] and s03.dollars_out_of_policy == Decimal("18.00")
    c05 = audit(con, "C05", "rules")   # client dinner: wine allowed, $560 under 4 x $150
    assert c05.codes == [] and c05.status == "OK"
    s07 = audit(con, "S07", "rules")
    assert s07.codes == ["AIRFARE_CLASS"] and s07.dollars_out_of_policy == Decimal("1020.00")
    g02 = audit(con, "G02", "rules")   # attendees blank
    assert g02.status == "NEEDS_HUMAN" and g02.dollars_out_of_policy is None
