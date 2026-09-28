"""stdlib-only CLI for the ASTRA pipeline."""
from __future__ import annotations

import argparse
import json
import sys

from astra.pipeline import PipelineCollisionError, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astra",
        description="Incrementally enrich host-agnostic JSONL event files.",
    )
    parser.add_argument(
        "--input",
        action="append",
        dest="inputs",
        required=True,
        help="Glob/wildcard pattern for JSONL event files. Repeatable.",
    )
    parser.add_argument("--enriched", required=True, help="Append-only lossless enriched JSONL.")
    parser.add_argument("--grouped", required=True, help="Compact grouped JSONL for reporting.")
    parser.add_argument("--state", required=True, help="Durable per-file cursor state JSON.")
    parser.add_argument("--policy", help="Optional external JSON severity policy.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_pipeline(
            patterns=args.inputs,
            enriched_path=args.enriched,
            grouped_path=args.grouped,
            state_path=args.state,
            policy_path=args.policy,
        )
    except PipelineCollisionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
