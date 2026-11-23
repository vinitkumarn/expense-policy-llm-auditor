"""naive | rules | fixed | agent side by side, from the eval JSON files.

`compare` never runs an auditor: it reads reports/eval_<by><suffix>.json written by `eval`.
An auditor with no file for the set shows "not run". The hard-case rows pick their lines from
the plan file (labels), never from rules code:
- X cases: line ids starting with "X" (the cross-reference group).
- Post-amendment Tier 1 dinners: lines whose expected citations include "Appendix C".
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from expaudit import config
from expaudit.evaluate import AUDITORS, json_path, load_labels

NOT_RUN = "not run"
AMENDMENT_ID = "Appendix C"


def _load(by: str, set_name: str, reports_dir: Path) -> dict | None:
    path = json_path(by, set_name, reports_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(v) -> str:
    return "n/a" if v is None else f"{float(v):.1%}"


def _usd(v, places: int = 2) -> str:
    return "unknown" if v is None else f"${Decimal(v):,.{places}f}"


def _model(data: dict, key: str, fmt) -> str:
    mm = data["metrics"].get("model")
    if mm is None:
        return "-"           # baselines: no model, no tool calls
    return fmt(mm.get(key))


ROWS = [
    ("Finding recall", lambda d: f"{_pct(d['metrics']['finding_recall'])} "
                                 f"({d['metrics']['found_findings']}/{d['metrics']['planted_findings']})"),
    ("False findings on clean lines", lambda d: f"{d['metrics']['clean_lines_with_false_findings']}"
                                                f" of {d['metrics']['clean_lines']}"),
    ("Dollars out of policy exact match", lambda d: _pct(d["metrics"]["dollars_exact_match"])),
    ("Dollars caught vs planted", lambda d: f"{_usd(d['metrics']['dollars_caught'])} of "
                                            f"{_usd(d['metrics']['dollars_planted'])}"),
    ("Unknown-dollar lines, expected / predicted",
     lambda d: f"{d['metrics']['lines_unknown_dollars_expected']} / "
               f"{d['metrics']['lines_unknown_dollars_predicted']}"),
    ("Status accuracy", lambda d: _pct(d["metrics"]["status_accuracy"])),
    ("Approver accuracy", lambda d: _pct(d["metrics"]["approver_accuracy"])),
    ("Actions accuracy", lambda d: _pct(d["metrics"]["actions_accuracy"])),
    ("Citation accuracy", lambda d: _model(d, "citation_accuracy", _pct)),
    ("Cost per line", lambda d: _model(d, "cost_per_line_usd", lambda v: _usd(v, 4))),
    ("Avg tool calls per line", lambda d: _model(
        d, "avg_tool_calls", lambda v: "n/a" if v is None else f"{float(v):.2f}")),
    ("Step-limit hits", lambda d: _model(d, "step_limit_hits", str)),
]


def _subset(data: dict, ids: list[str]) -> str:
    if not ids:
        return "n/a"
    by_id = {row["line_id"]: row for row in data["lines"]}
    rows = [by_id[i] for i in ids if i in by_id]
    if not rows:
        return "n/a"
    ok = sum(r["all_ok"] for r in rows)
    dollars = sum(r["dollars_ok"] for r in rows)
    return f"{ok}/{len(rows)} correct, {dollars}/{len(rows)} dollars exact"


def hard_case_ids(set_name: str, data_dir: Path | None = None) -> dict[str, list[str]]:
    labels = load_labels(set_name, data_dir)
    return {
        "X cases (cross-reference)": [lab["line_id"] for lab in labels
                                      if lab["line_id"].startswith("X")],
        "Post-amendment Tier 1 dinners (label cites Appendix C)": [
            lab["line_id"] for lab in labels
            if any(AMENDMENT_ID in ids for ids in lab["expected_cited_clause_ids"].values())],
    }


def render(set_name: str, results: dict[str, dict | None], hard: dict[str, list[str]]) -> str:
    head = "| Metric | " + " | ".join(AUDITORS) + " |"
    sep = "|---|" + "---|" * len(AUDITORS)
    lines = [
        f"# Compare: {' | '.join(AUDITORS)} on the {set_name} set",
        "",
        "Read from the eval JSON files in this folder. \"not run\" = no eval for that auditor "
        "on this set yet. Labels come from the plan files only."
        + (" This set is never used to tune prompts or rules." if set_name != "main" else ""),
        "",
        head, sep,
    ]
    for name, fn in ROWS:
        cells = [NOT_RUN if results[a] is None else fn(results[a]) for a in AUDITORS]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Hard cases", "", "| Lines | " + " | ".join(AUDITORS) + " |", sep]
    for name, ids in hard.items():
        label = f"{name}: {', '.join(ids)}" if ids else name
        cells = [NOT_RUN if results[a] is None else _subset(results[a], ids) for a in AUDITORS]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def compare(set_name: str = "main", data_dir: Path | None = None,
            reports_dir: Path | None = None) -> Path:
    reports_dir = Path(reports_dir or config.REPORTS_DIR)
    results = {by: _load(by, set_name, reports_dir) for by in AUDITORS}
    text = render(set_name, results, hard_case_ids(set_name, data_dir))
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"compare_{set_name}.md"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
