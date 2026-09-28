"""Deterministic CLI over the ASTRA review store; no model calls, no scheduler.

Read-only with respect to evidence sources: it ingests retained enriched
buckets and never rewrites them. All validation failures exit nonzero with a
machine-readable empty stdout and a message on stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
import sqlite3

from astra.review import ReviewStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m astra.review_cli",
        description="Deterministic candidate-review store CLI (no model calls).",
    )
    parser.add_argument("--root", required=True,
                        help="Directory holding enriched-*.jsonl evidence buckets (read-only).")
    parser.add_argument("--state", required=True, help="Path to review.sqlite state file.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="Scan changed buckets and ingest new rows idempotently.")
    p.add_argument("--now", type=float, default=None, help="Epoch seconds override (tests).")

    p = sub.add_parser("prepare", help="Build a bounded review packet for triage or RCA.")
    p.add_argument("kind", choices=["triage", "rca", "rca-a", "rca-b"])
    p.add_argument("--max-bytes", type=int, default=12000, help="Packet byte budget.")
    p.add_argument("--now", type=float, default=None)

    p = sub.add_parser("complete", help="Record a validated result for a prepared batch.")
    p.add_argument("--batch", required=True)
    p.add_argument("--result", help="JSON file with triage decisions for every finding.")
    p.add_argument("--report", help="Markdown RCA report file (contract-validated).")
    p.add_argument("--archive-dir", help="Host-local archive directory for the report.")
    p.add_argument("--shared-dir", help="Optional configurable shared-copy directory.")
    p.add_argument("--now", type=float, default=None)

    p = sub.add_parser("evidence", help="Resolve a finding/evidence ID to a bounded excerpt.")
    p.add_argument("--id", required=True)
    p.add_argument("--max-bytes", type=int, default=8192)

    p = sub.add_parser("status", help="Pending counts, leases, dispatch booleans, early trigger.")
    p.add_argument("--now", type=float, default=None)

    p = sub.add_parser("history", help="Bounded RCA and reviewed-finding history.")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("retention", help="Report unsynced/missing sources and evidence gaps.")
    p.add_argument("--now", help="ISO timestamp override (tests).")

    p = sub.add_parser("prune", help="Expire evidence older than 72h; mark unreviewed explicitly.")
    p.add_argument("--now", help="ISO timestamp override (tests).")

    p = sub.add_parser("recover", help="Explicitly release retained unfinished work from a blocked batch.")
    p.add_argument("--batch", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--now", type=float, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = None
    try:
        store = ReviewStore(args.state)
        if args.command == "sync":
            result = store.sync_sources(args.root, now=args.now)
        elif args.command == "prepare":
            result = store.prepare(args.kind, now=args.now, max_bytes=args.max_bytes)
        elif args.command == "complete":
            batch = store.db.execute('SELECT kind FROM batches WHERE id=?', (args.batch,)).fetchone()
            if batch is None:
                raise ValueError(f"Unknown batch {args.batch}")
            if batch["kind"] in ("rca", "rca-a", "rca-b"):
                if not args.report:
                    raise ValueError("RCA completion requires --report FILE")
                report_text = open(args.report, encoding="utf-8").read()
                result = store.complete_rca(args.batch, report_text,
                                            archive_dir=args.archive_dir,
                                            shared_dir=args.shared_dir, now=args.now)
            else:
                if not args.result:
                    raise ValueError("Triage completion requires --result FILE")
                findings = json.loads(open(args.result, encoding="utf-8").read())
                result = store.complete(args.batch, findings, now=args.now)
        elif args.command == "recover":
            result = store.recover_blocked_batch(args.batch, args.reason, now=args.now)
        elif args.command == "evidence":
            result = store.get_evidence(args.id, max_bytes=args.max_bytes)
        elif args.command == "status":
            result = store.status(now=args.now)
        elif args.command == "history":
            result = store.get_history(limit=args.limit)
        elif args.command == "retention":
            result = store.retention_report(args.root, now=args.now)
        elif args.command == "prune":
            result = store.prune_evidence(now=args.now)
        else:  # pragma: no cover - argparse enforces choices
            raise ValueError(f"Unsupported command {args.command}")
    except (ValueError, OSError, RuntimeError, sqlite3.OperationalError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if store is not None:store.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok', True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
