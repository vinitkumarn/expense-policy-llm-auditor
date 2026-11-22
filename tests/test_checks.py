from decimal import Decimal

from conftest import make_line
from expaudit import db
from expaudit.checks import duplicate, missing_receipt, run_checks


def test_receipt_at_exactly_75_is_fine():
    assert missing_receipt(make_line(amount=Decimal("75.00"), receipt_attached=False)) is None


def test_receipt_at_75_01_is_missing():
    f = missing_receipt(make_line(amount=Decimal("75.01"), receipt_attached=False))
    assert f is not None and f.code == "MISSING_RECEIPT" and f.cites == ("3.1",)


def test_receipt_attached_is_fine():
    assert missing_receipt(make_line(amount=Decimal("500.00"), receipt_attached=True)) is None


def test_duplicates_found_only_on_the_later_line(con):
    for lid in ["S06", "A03"]:
        f = duplicate(con, db.load_line(con, lid))
        assert f is not None and f.code == "DUPLICATE" and f.cites == ("3.5",)
    for lid in ["S06-ORIG", "A03-ORIG", "C01", "S05"]:
        assert duplicate(con, db.load_line(con, lid)) is None


def test_run_checks_on_planted_lines(con):
    assert [f.code for f in run_checks(con, db.load_line(con, "S05"))] == ["MISSING_RECEIPT"]
    assert [f.code for f in run_checks(con, db.load_line(con, "C02"))] == []   # $65, no receipt
    assert [f.code for f in run_checks(con, db.load_line(con, "R08"))] == ["MISSING_RECEIPT"]
