from __future__ import annotations

import argparse
from pathlib import Path

from ai_native.factory_runner.certification_schema_generation import (
    CERTIFICATION_SCHEMA_DIRECTORY,
    CERTIFICATION_SCHEMAS,
    SCHEMA_SET_DIGEST_FILENAME,
    certification_schema_artifact_drift,
    write_certification_schema_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate deterministic factory-runner release certification schemas."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail if checked-in certification artifacts differ",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="write certification schema artifacts (the default)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=CERTIFICATION_SCHEMA_DIRECTORY,
        help="override the certification schema output directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    if not args.check:
        write_certification_schema_artifacts(output_dir)

    differences = certification_schema_artifact_drift(output_dir)
    if differences:
        print("factory runner certification schema drift detected:")
        for difference in differences:
            print(f"- {difference}")
        return 1

    digest = (
        (output_dir / SCHEMA_SET_DIGEST_FILENAME).read_text(encoding="ascii").strip()
    )
    action = "verified" if args.check else "generated"
    print(
        f"{action} {len(CERTIFICATION_SCHEMAS)} certification schemas; "
        f"schema set {digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
