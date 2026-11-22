"""Command line.

python -m expaudit build [--out DIR]
python -m expaudit run --line S01 --by naive|rules
python -m expaudit eval --by naive|rules [--set main|redteam|reworded] [--cases S01,S02] [--limit N]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from expaudit import config, db, generator
from expaudit.baselines import BASELINES, audit
from expaudit.evaluate import SETS, load_labels, run_eval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="expaudit", description="T&E expense policy auditor")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="write synthetic CSVs, the plan files and the DuckDB")
    build.add_argument("--out", type=Path, default=config.DATA_DIR,
                       help="output folder (default: data/)")

    run = sub.add_parser("run", help="audit one line and print the decision")
    run.add_argument("--line", required=True)
    run.add_argument("--by", required=True, choices=sorted(BASELINES))
    run.add_argument("--data", type=Path, default=config.DATA_DIR, help=argparse.SUPPRESS)

    ev = sub.add_parser("eval", help="score an auditor against the plan files")
    ev.add_argument("--by", required=True, choices=sorted(BASELINES))
    ev.add_argument("--set", choices=list(SETS), help="default: all three sets")
    ev.add_argument("--cases", help="comma-separated line ids, e.g. S01,X09")
    ev.add_argument("--limit", type=int)
    ev.add_argument("--data", type=Path, default=config.DATA_DIR, help=argparse.SUPPRESS)
    ev.add_argument("--reports", type=Path, default=config.REPORTS_DIR, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    if args.command == "build":
        for path in generator.build(args.out):
            print(f"wrote {path}")
        db_path = Path(args.out) / db.DB_FILE
        db.build_db(Path(args.out) / config.CSV_DIR_NAME, db_path).close()
        print(f"wrote {db_path}")
        return 0

    try:
        if args.command == "run":
            con = db.connect(args.data / db.DB_FILE)
            try:
                decision = audit(con, args.line, args.by)
            finally:
                con.close()
            print(json.dumps(decision.to_dict(), indent=2))
            return 0

        if args.command == "eval":
            cases = [c.strip() for c in args.cases.split(",")] if args.cases else None
            set_names = [args.set] if args.set else list(SETS)
            for set_name in set_names:
                set_cases = cases
                if cases and not args.set:   # only the sets that hold the requested ids
                    ids = {lab["line_id"] for lab in load_labels(set_name, args.data)}
                    set_cases = [c for c in cases if c in ids]
                    if not set_cases:
                        continue
                print(f"== {args.by} on {set_name} ==")
                _, path = run_eval(args.by, set_name, cases=set_cases, limit=args.limit,
                                   data_dir=args.data, reports_dir=args.reports)
                print(f"wrote {path}")
            return 0
    except (FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
