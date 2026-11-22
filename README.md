# expense-policy-llm-auditor

A T&E expense policy auditor built as a portfolio project. For each expense line it finds the
travel-policy clauses that apply, says whether the line breaks one, cites the exact clause, and
shows the dollars out of policy, the approver and the action. It **suggests**; a human decides.
Synthetic data only, USD only, local DuckDB. The design is in [docs/design.md](docs/design.md).

Status: **PR 1**. This PR adds the skeleton, the synthetic policy (`data/policy.md`), the policy
parser and the planted test data. The model paths come in later PRs.

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
src/expaudit/__main__.py  command line (python -m expaudit build)
tests/                    pytest tests
```
