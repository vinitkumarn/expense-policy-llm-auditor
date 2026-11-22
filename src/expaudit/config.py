"""Settings in one place. Finance owns the thresholds; money is always Decimal."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Paths --------------------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
POLICY_PATH = DATA_DIR / "policy.md"
CSV_DIR_NAME = "csv"
CSV_DIR = DATA_DIR / CSV_DIR_NAME
PLAN_FILE = "plan.json"
REDTEAM_PLAN_FILE = "redteam_plan.json"
REWORDED_PLAN_FILE = "reworded_plan.json"
PLAN_PATH = DATA_DIR / PLAN_FILE
REDTEAM_PLAN_PATH = DATA_DIR / REDTEAM_PLAN_FILE
REWORDED_PLAN_PATH = DATA_DIR / REWORDED_PLAN_FILE
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "expaudit.duckdb"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

load_dotenv(ROOT / ".env")

# Data set -----------------------------------------------------------------------------------
REFERENCE_DATE = date(2026, 11, 2)
SEED = 20261102
CURRENCY = "USD"

# Policy thresholds used by Python (the policy text stays the source of truth for limits) -----
RECEIPT_THRESHOLD = Decimal("75")   # policy 3.1: receipt required for a line over $75
MANAGER_LIMIT = Decimal("250")      # policy 8.1: under $250 -> manager, else Finance

# Rules baseline only ------------------------------------------------------------------------
# A hardcoded limits table, copied by hand from Appendix B of the policy. It was written BEFORE
# amendment C-1 (Appendix C, Tier 1 dinner $85 from 2026-10-01) and does not know about it, on
# purpose: this is what a hand-maintained rules table looks like when the policy moves on. The
# model paths read the policy text instead. Never used by decide.py or the model paths.
RULES_TIER1_CITIES = frozenset({"San Francisco", "New York", "London", "Boston", "Seattle"})
RULES_LIMITS = {
    ("dinner", 1): Decimal("100"),
    ("dinner", 2): Decimal("70"),
    ("other_meal", 1): Decimal("40"),
    ("other_meal", 2): Decimal("40"),
    ("lodging", 1): Decimal("300"),
    ("lodging", 2): Decimal("200"),
    ("client", 1): Decimal("150"),     # policy 4.3, per person, alcohol included
    ("client", 2): Decimal("150"),
}
RULES_BUSINESS_CLASS_MIN_HOURS = Decimal("6")   # policy 6.1: business only when over 6 hours

# Model paths --------------------------------------------------------------------------------
AGENT_MAX_STEPS = 4      # max search_policy tool calls per line
RETRIEVAL_TOP_K = 3      # clauses returned by one policy search
MODEL = os.getenv("EXPAUDIT_MODEL") or "claude-sonnet-5-5"
MAX_TOKENS = 4000
