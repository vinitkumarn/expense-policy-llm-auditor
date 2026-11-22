from datetime import date
from decimal import Decimal

import pytest

from expaudit import db
from expaudit.db import Line
from expaudit.generator import build


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    """Planted data + DuckDB built once into a temp folder (never touches data/)."""
    out = tmp_path_factory.mktemp("data")
    build(out)
    db.build_db(out / "csv", out / db.DB_FILE).close()
    return out


@pytest.fixture()
def con(data_dir):
    connection = db.connect(data_dir / db.DB_FILE)
    yield connection
    connection.close()


def make_line(**overrides) -> Line:
    """A clean SUBMITTED Tier 2 dinner for one STAFF employee; override any field."""
    fields = dict(
        line_id="T01", report_id="RPT-T01", employee_id="E104", grade="STAFF",
        report_status="SUBMITTED", submitted_date=date(2026, 10, 20),
        transaction_date=date(2026, 10, 15), merchant="Test Kitchen", category="Meals",
        amount=Decimal("50.00"), city="Denver", nights=None, attendees_count=1,
        receipt_attached=True, memo="Dinner.",
    )
    fields.update(overrides)
    return Line(**fields)
