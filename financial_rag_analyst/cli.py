from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from financial_rag_analyst.app import bootstrap_application


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="financial-rag",
        description="Financial Agentic RAG analyst scaffold CLI.",
    )
    subparsers = parser.add_subparsers(dest="command")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Validate the local scaffold and load default application settings.",
    )
    bootstrap_parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Override the repository root used for bootstrap checks.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "bootstrap":
        application = bootstrap_application(repo_root=args.repo_root)
        print(f"{application.name} bootstrap ready at {application.repo_root}")
        return 0

    parser.print_help()
    return 0
