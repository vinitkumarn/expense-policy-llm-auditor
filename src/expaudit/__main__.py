"""Command line: python -m expaudit build [--out DIR]"""

from __future__ import annotations

import argparse
from pathlib import Path

from expaudit import config, generator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="expaudit", description="T&E expense policy auditor")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="write synthetic CSVs and the three plan files")
    build.add_argument("--out", type=Path, default=config.DATA_DIR,
                       help="output folder (default: data/)")
    args = parser.parse_args(argv)

    if args.command == "build":
        for path in generator.build(args.out):
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
