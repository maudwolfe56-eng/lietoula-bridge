#!/usr/bin/env python3
"""Merge the base company seed with incremental expansion files, then audit pending entries."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_seed(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("companies") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"invalid seed file: {path}")
    return [row for row in rows if isinstance(row, dict)]


def merge_seeds(base_path: Path, expansion_glob: str) -> tuple[list[dict[str, Any]], list[str]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    sources: list[str] = []
    paths = [base_path, *sorted(base_path.parent.glob(expansion_glob))]
    for path in paths:
        if not path.exists():
            continue
        sources.append(path.name)
        for row in load_seed(path):
            company_id = str(row.get("company_id") or "").strip()
            if not company_id or company_id in seen:
                continue
            seen.add(company_id)
            merged.append(row)
    return merged, sources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="company_seed_1000.json")
    parser.add_argument("--expansion-glob", default="company_seed_expansion_*.json")
    parser.add_argument("--merged-seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--start-index", type=int)
    args = parser.parse_args()

    base_path = Path(args.seed_file)
    merged, sources = merge_seeds(base_path, args.expansion_glob)
    merged_path = Path(args.merged_seed_file)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "target_count": 1000,
                "seed_status": "incrementally_expanded_auditable_seed",
                "valid_seed_count": len(merged),
                "source_files": sources,
                "companies": merged,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    cmd = [
        sys.executable,
        "crawl_company_jobs_pending.py",
        "--seed-file",
        str(merged_path),
        "--output-dir",
        args.output_dir,
        "--state-file",
        args.state_file,
        "--batch-size",
        str(args.batch_size),
    ]
    if args.start_index is not None:
        cmd.extend(["--start-index", str(args.start_index)])
    print(json.dumps({"merged_seed_count": len(merged), "sources": sources}, ensure_ascii=False))
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
