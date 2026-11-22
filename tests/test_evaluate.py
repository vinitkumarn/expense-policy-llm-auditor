import json
from decimal import Decimal

import pytest

from expaudit import config
from expaudit.__main__ import main
from expaudit.evaluate import load_labels, run_eval


def test_labels_come_from_the_plan_files(data_dir):
    labels = load_labels("main", data_dir)
    assert len(labels) == 46
    assert all(not lab["line_id"].endswith("-ORIG") for lab in labels)
    assert len(load_labels("redteam", data_dir)) == 8
    assert len(load_labels("reworded", data_dir)) == 8


@pytest.mark.parametrize("by", ["naive", "rules"])
@pytest.mark.parametrize("set_name, suffix", [("main", ""), ("redteam", "_redteam"),
                                              ("reworded", "_reworded")])
def test_eval_writes_report_per_set(data_dir, tmp_path, by, set_name, suffix):
    m, path = run_eval(by, set_name, data_dir=data_dir, reports_dir=tmp_path, echo=lambda *_: None)
    assert path == tmp_path / f"eval_{by}{suffix}.md"
    text = path.read_text(encoding="utf-8")
    assert "Finding recall" in text and "Lines with unknown dollars" in text
    for key in ["dollars_exact_match", "dollars_held_match", "status_accuracy",
                "approver_accuracy", "actions_accuracy"]:
        assert 0 <= m[key] <= 1
    assert m["dollars_caught"] <= m["dollars_planted"]


def test_naive_main_recall_is_python_codes_only(data_dir, tmp_path):
    m, _ = run_eval("naive", "main", data_dir=data_dir, reports_dir=tmp_path, echo=lambda *_: None)
    labels = load_labels("main", data_dir)
    python_codes = sum(c in ("MISSING_RECEIPT", "DUPLICATE")
                       for lab in labels for c in lab["expected_codes"])
    assert m["found_findings"] == python_codes
    assert m["clean_lines_with_false_findings"] == 0
    assert m["dollars_held_match"] == 1
    assert m["lines_unknown_dollars_expected"] == 4    # G01, G02, G03, G05


def test_cases_and_limit_and_progress(data_dir, tmp_path):
    seen = []
    m, _ = run_eval("rules", "main", cases=["S01", "M03"], data_dir=data_dir,
                    reports_dir=tmp_path, echo=seen.append)
    assert m["lines"] == 2 and len(seen) == 2 and seen[0].startswith("[1/2] S01")
    m, _ = run_eval("rules", "main", limit=3, data_dir=data_dir, reports_dir=tmp_path,
                    echo=lambda *_: None)
    assert m["lines"] == 3
    with pytest.raises(KeyError):
        run_eval("rules", "main", cases=["R01"], data_dir=data_dir, reports_dir=tmp_path)


def test_cli_run_and_eval(data_dir, tmp_path, capsys):
    assert main(["run", "--line", "M03", "--by", "rules", "--data", str(data_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dollars_out_of_policy"] == "120.00" and out["dollars_held"] == "720.00"
    assert out["actions"] == ["REJECT_LINE_BEFORE_PAYMENT", "HOLD_LINE_FOR_RECEIPT"]

    assert main(["eval", "--by", "naive", "--data", str(data_dir),
                 "--reports", str(tmp_path)]) == 0
    for name in ["eval_naive.md", "eval_naive_redteam.md", "eval_naive_reworded.md"]:
        assert (tmp_path / name).exists()
    assert main(["eval", "--by", "rules", "--cases", "R05", "--data", str(data_dir),
                 "--reports", str(tmp_path)]) == 0
    assert (tmp_path / "eval_rules_redteam.md").exists()


def test_cli_missing_db_is_friendly(tmp_path, capsys):
    assert main(["run", "--line", "S01", "--by", "rules", "--data", str(tmp_path)]) == 2
    assert "python -m expaudit build" in capsys.readouterr().out
