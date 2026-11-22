"""Score an auditor against the planted answer keys.

Labels come ONLY from the plan files written by the generator (plan.json, redteam_plan.json,
reworded_plan.json), never from rules code. The red-team and reworded sets are reported
separately and are never used to tune prompts or rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from expaudit import config
from expaudit.baselines import audit
from expaudit.db import DB_FILE, connect
from expaudit.decide import ACTION_ORDER, Decision

SETS = {
    "main": (config.PLAN_FILE, ""),
    "redteam": (config.REDTEAM_PLAN_FILE, "_redteam"),
    "reworded": (config.REWORDED_PLAN_FILE, "_reworded"),
}


def _money(value) -> Decimal | None:
    return None if value is None else Decimal(value)


def _canonical(actions: list[str]) -> list[str]:
    return sorted(actions, key=lambda a: ACTION_ORDER.index(a) if a in ACTION_ORDER else 99)


def load_labels(set_name: str, data_dir: Path | None = None) -> list[dict]:
    """The answer key for one set (main excludes the duplicate 'support_lines')."""
    data_dir = Path(data_dir or config.DATA_DIR)
    plan = json.loads((data_dir / SETS[set_name][0]).read_text(encoding="utf-8"))
    return plan["cases"]


@dataclass
class LineResult:
    label: dict
    decision: Decision
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    dollars_ok: bool = False
    held_ok: bool = False
    status_ok: bool = False
    approver_ok: bool = False
    actions_ok: bool = False

    @property
    def all_ok(self) -> bool:
        return (not self.missed and not self.extra and self.dollars_ok and self.held_ok
                and self.status_ok and self.approver_ok and self.actions_ok)


def score_line(label: dict, decision: Decision) -> LineResult:
    expected, got = label["expected_codes"], decision.codes
    return LineResult(
        label=label, decision=decision,
        found=[c for c in expected if c in got],
        missed=[c for c in expected if c not in got],
        extra=[c for c in got if c not in expected],
        dollars_ok=_money(label["dollars_out_of_policy"]) == decision.dollars_out_of_policy,
        held_ok=_money(label["dollars_held"]) == decision.dollars_held,
        status_ok=label["status"] == decision.status,
        approver_ok=label["approver"] == decision.approver,
        actions_ok=_canonical(label["actions"]) == _canonical(decision.actions),
    )


def metrics(results: list[LineResult]) -> dict:
    n = len(results)
    planted = sum(len(r.label["expected_codes"]) for r in results)
    found = sum(len(r.found) for r in results)
    clean = [r for r in results if not r.label["expected_codes"] and r.label["status"] == "OK"]

    planted_dollars = Decimal("0.00")
    caught_dollars = Decimal("0.00")
    for r in results:
        for code, exp in r.label["dollars_by_code"].items():
            exp = _money(exp)
            if exp is None:
                continue
            planted_dollars += exp
            got = r.decision.dollars_by_code.get(code)
            if got is not None:
                caught_dollars += min(got, exp)

    def total(values):   # totals skip unknown (None) dollars
        return sum((v for v in values if v is not None), Decimal("0.00"))

    return {
        "lines": n,
        "planted_findings": planted,
        "found_findings": found,
        "finding_recall": found / planted if planted else None,
        "clean_lines": len(clean),
        "clean_lines_with_false_findings": sum(1 for r in clean if r.decision.codes),
        "false_findings_all_lines": sum(len(r.extra) for r in results),
        "dollars_exact_match": sum(r.dollars_ok for r in results) / n if n else None,
        "dollars_planted": planted_dollars,
        "dollars_caught": caught_dollars,
        "dollars_out_of_policy_expected_total":
            total(_money(r.label["dollars_out_of_policy"]) for r in results),
        "dollars_out_of_policy_predicted_total":
            total(r.decision.dollars_out_of_policy for r in results),
        "lines_unknown_dollars_expected":
            sum(r.label["dollars_out_of_policy"] is None for r in results),
        "lines_unknown_dollars_predicted":
            sum(r.decision.dollars_out_of_policy is None for r in results),
        "dollars_held_match": sum(r.held_ok for r in results) / n if n else None,
        "status_accuracy": sum(r.status_ok for r in results) / n if n else None,
        "approver_accuracy": sum(r.approver_ok for r in results) / n if n else None,
        "actions_accuracy": sum(r.actions_ok for r in results) / n if n else None,
        "lines_fully_correct": sum(r.all_ok for r in results),
    }


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt(value) -> str:
    return "unknown" if value is None else f"${value:,.2f}"


def render_report(by: str, set_name: str, results: list[LineResult], m: dict) -> str:
    lines = [
        f"# Eval: {by} on the {set_name} set",
        "",
        "Labels come from the plan files only. "
        + ("This set is reported separately and is never used to tune prompts or rules."
           if set_name != "main" else "Red-team and reworded sets are in separate reports."),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Lines | {m['lines']} |",
        f"| Finding recall | {_pct(m['finding_recall'])} "
        f"({m['found_findings']} of {m['planted_findings']}) |",
        f"| False findings on clean lines | {m['clean_lines_with_false_findings']} of "
        f"{m['clean_lines']} clean lines |",
        f"| False findings, all lines (extra codes) | {m['false_findings_all_lines']} |",
        f"| Dollars out of policy exact match | {_pct(m['dollars_exact_match'])} |",
        f"| Dollars caught vs planted | {_fmt(m['dollars_caught'])} of "
        f"{_fmt(m['dollars_planted'])} |",
        f"| Dollars out of policy total, expected / predicted (unknown skipped) | "
        f"{_fmt(m['dollars_out_of_policy_expected_total'])} / "
        f"{_fmt(m['dollars_out_of_policy_predicted_total'])} |",
        f"| Lines with unknown dollars, expected / predicted | "
        f"{m['lines_unknown_dollars_expected']} / {m['lines_unknown_dollars_predicted']} |",
        f"| Dollars held match | {_pct(m['dollars_held_match'])} |",
        f"| Status accuracy | {_pct(m['status_accuracy'])} |",
        f"| Approver accuracy | {_pct(m['approver_accuracy'])} |",
        f"| Actions accuracy | {_pct(m['actions_accuracy'])} |",
        f"| Lines fully correct | {m['lines_fully_correct']} of {m['lines']} |",
        "",
        "## Per line",
        "",
        "| Line | Expected codes | Got codes | Out of policy exp / got | Held exp / got "
        "| Approver exp / got | Actions exp / got | Status exp / got | OK |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lab, d = r.label, r.decision
        lines.append(
            f"| {lab['line_id']} | {', '.join(lab['expected_codes']) or '-'} "
            f"| {', '.join(d.codes) or '-'} "
            f"| {_fmt(_money(lab['dollars_out_of_policy']))} / {_fmt(d.dollars_out_of_policy)} "
            f"| {_fmt(_money(lab['dollars_held']))} / {_fmt(d.dollars_held)} "
            f"| {lab['approver']} / {d.approver} "
            f"| {', '.join(lab['actions']) or '-'} / {', '.join(d.actions) or '-'} "
            f"| {lab['status']} / {d.status} | {'yes' if r.all_ok else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def run_eval(by: str, set_name: str = "main", cases: list[str] | None = None,
             limit: int | None = None, data_dir: Path | None = None,
             reports_dir: Path | None = None, echo=print) -> tuple[dict, Path]:
    data_dir = Path(data_dir or config.DATA_DIR)
    reports_dir = Path(reports_dir or config.REPORTS_DIR)
    labels = load_labels(set_name, data_dir)
    if cases:
        wanted = set(cases)
        unknown = wanted - {lab["line_id"] for lab in labels}
        if unknown:
            raise KeyError(f"not in the {set_name} set: {sorted(unknown)}")
        labels = [lab for lab in labels if lab["line_id"] in wanted]
    if limit is not None:
        labels = labels[:limit]

    con = connect(data_dir / DB_FILE)
    results = []
    try:
        for i, label in enumerate(labels, 1):
            r = score_line(label, audit(con, label["line_id"], by))
            results.append(r)
            echo(f"[{i}/{len(labels)}] {label['line_id']}  expected={label['expected_codes']} "
                 f"got={r.decision.codes}  {'ok' if r.all_ok else 'diff'}")
    finally:
        con.close()

    m = metrics(results)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"eval_{by}{SETS[set_name][1]}.md"
    path.write_text(render_report(by, set_name, results, m), encoding="utf-8", newline="\n")
    return m, path
