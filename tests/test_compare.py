"""compare: naive | rules | fixed | agent side by side; missing reports show "not run"."""

from expaudit.__main__ import main
from expaudit.compare import compare, hard_case_ids
from expaudit.evaluate import run_eval


def _row(text, name):
    return next(line for line in text.splitlines() if line.startswith(f"| {name}"))


def test_compare_with_baselines_only(data_dir, sandbox):
    reports = sandbox / "reports"
    for by in ("naive", "rules"):
        run_eval(by, "main", data_dir=data_dir, reports_dir=reports, echo=lambda *_: None)
    path = compare("main", data_dir, reports)
    assert path.name == "compare_main.md"
    text = path.read_text(encoding="utf-8")
    assert "| Metric | naive | rules | fixed | agent |" in text
    recall = _row(text, "Finding recall").split("|")
    assert "not run" not in recall[2] and "not run" not in recall[3]
    assert recall[4].strip() == "not run" and recall[5].strip() == "not run"
    assert _row(text, "Avg tool calls per line").split("|")[2].strip() == "-"
    for name in ("False findings on clean lines", "Dollars out of policy exact match",
                 "Dollars caught vs planted", "Unknown-dollar lines", "Status accuracy",
                 "Approver accuracy", "Actions accuracy", "Citation accuracy", "Cost per line",
                 "Step-limit hits"):
        assert _row(text, name)
    x_row = _row(text, "X cases")
    assert "X01" in x_row and "X09" in x_row and "correct" in x_row and "not run" in x_row
    assert _row(text, "Post-amendment Tier 1 dinners")


def test_post_amendment_rows_come_from_the_plan_labels(data_dir):
    ids = hard_case_ids("main", data_dir)
    assert len(ids["X cases (cross-reference)"]) == 9
    post = ids["Post-amendment Tier 1 dinners (label cites Appendix C)"]
    assert {"X01", "X02", "X08", "X09"} <= set(post) and "X03" not in post


def test_cli_compare_with_no_reports_is_all_not_run(data_dir, sandbox, capsys):
    reports = sandbox / "reports"
    assert main(["compare", "--set", "redteam", "--data", str(data_dir),
                 "--reports", str(reports)]) == 0
    assert "compare_redteam.md" in capsys.readouterr().out
    text = (reports / "compare_redteam.md").read_text(encoding="utf-8")
    assert _row(text, "Finding recall").count("not run") == 4
    assert "never used to tune" in text
