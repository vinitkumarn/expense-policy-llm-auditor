# expense-policy-llm-auditor

A T&E expense policy auditor built as a portfolio project. For each expense line it finds the
travel-policy clauses that apply, says whether the line breaks one, cites the exact clause, and
shows the dollars out of policy, the approver and the action. It **suggests**; a human decides.
Synthetic data only, USD only, local DuckDB. The design is in [docs/design.md](docs/design.md).

Status: **PR 2**. PR 1 added the skeleton, the synthetic policy (`data/policy.md`), the policy
parser and the planted test data. PR 2 adds DuckDB loading, the Python checks, the decision
logic, the naive and rules baselines and the eval. The model paths come in later PRs.

## Setup on Windows (PowerShell, Python 3.14 via the py launcher)

```powershell
cd expense-policy-llm-auditor
py -3.14 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
copy .env.example .env    # add ANTHROPIC_API_KEY later; not needed for PR 1
```

## Build the synthetic data

```powershell
python -m expaudit build
```

This writes (all ignored by git, rebuilt any time, identical bytes every run):

| File | What it holds |
|---|---|
| `data/csv/employees.csv` | 12 employees (STAFF / MANAGER / DIRECTOR) |
| `data/csv/reports.csv` | one report per case line, SUBMITTED or PAID |
| `data/csv/expense_lines.csv` | 46 main + 8 red-team + 8 reworded lines + 2 "original" lines for the duplicates |
| `data/plan.json` | answer key for the 46 main cases (+ `support_lines` for the 2 originals) |
| `data/redteam_plan.json` | answer key for the 8 red-team cases |
| `data/reworded_plan.json` | answer key for the 8 reworded cases, with `planted_phrases` |

`build` also loads the CSVs into `data/expaudit.duckdb` (money as `DECIMAL(18,2)`, dates as `DATE`).

## Audit one line, run the eval

```powershell
python -m expaudit run --line M03 --by rules
python -m expaudit eval --by naive
python -m expaudit eval --by rules
python -m expaudit eval --by rules --set main --cases S01,X09
python -m expaudit eval --by rules --set redteam --limit 3
```

`eval` writes `reports/eval_<by>.md`, `reports/eval_<by>_redteam.md` and
`reports/eval_<by>_reworded.md`. Labels come only from the plan files. The red-team and reworded
sets are reported separately and are never used to tune anything.

Two baselines, no model:

- **naive**: the Python checks only (missing receipt over $75, duplicate line).
- **rules**: naive + a hardcoded limits table + memo keyword lists + regexes for attendees and
  flight time. The limits table (`RULES_LIMITS` in `config.py`) was copied from Appendix B
  **before amendment C-1**, so it still uses the $100 Tier 1 dinner limit and misses dinners
  that are over the $85 amended limit from 2026-10-01. That is on purpose: it shows what a
  hand-kept rules table does when the policy changes.

## Run the tests

```powershell
python -m pytest -q
```

## Layout

```
data/policy.md            synthetic travel policy (committed; the source of truth for limits)
src/expaudit/config.py    thresholds, dates, model settings, paths
src/expaudit/policy.py    policy.md -> clauses + policy hash
src/expaudit/generator.py planted cases and their labels (the eval answer key)
src/expaudit/db.py        CSVs -> DuckDB with real types; load_line(line_id)
src/expaudit/checks.py    MISSING_RECEIPT and DUPLICATE (always run, never dropped)
src/expaudit/decide.py    Python decisions: dollars, difference type, approver, actions, status
src/expaudit/baselines.py naive and rules baselines
src/expaudit/evaluate.py  scores against the plan files, writes reports/
src/expaudit/__main__.py  command line (python -m expaudit build)
tests/                    pytest tests
```
