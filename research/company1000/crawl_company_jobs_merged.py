#!/usr/bin/env python3
"""Merge the base company seed with incremental expansion files, then audit pending entries.

The merge is conservative: a legal entity must be unique by ``company_id`` and by a
normalized exact company name. Duplicate seed rows are recorded for audit instead of
silently inflating the 1,000-company target.
"""
from __future__ import annotations

import argparse
import json
import re
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


def normalize_company_name(value: Any) -> str:
    """Normalize spacing and punctuation without collapsing distinct legal entities."""
    text = str(value or "").strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[·•,，。.;；:：'\"“”‘’()（）\[\]【】]", "", text)
    return text


def merge_seeds(
    base_path: Path,
    expansion_glob: str,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    seen_ids: dict[str, dict[str, Any]] = {}
    seen_names: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    duplicates: list[dict[str, Any]] = []
    paths = [base_path, *sorted(base_path.parent.glob(expansion_glob))]

    for path in paths:
        if not path.exists():
            continue
        sources.append(path.name)
        for row_number, row in enumerate(load_seed(path), start=1):
            company_id = str(row.get("company_id") or "").strip()
            company_name = str(row.get("company_name") or "").strip()
            normalized_name = normalize_company_name(company_name)

            if not company_id:
                duplicates.append(
                    {
                        "source_file": path.name,
                        "source_row": row_number,
                        "skipped_company_id": None,
                        "skipped_company_name": company_name or None,
                        "reason": "missing_company_id",
                    }
                )
                continue

            if company_id in seen_ids:
                kept = seen_ids[company_id]
                duplicates.append(
                    {
                        "source_file": path.name,
                        "source_row": row_number,
                        "skipped_company_id": company_id,
                        "skipped_company_name": company_name or None,
                        "kept_company_id": kept.get("company_id"),
                        "kept_company_name": kept.get("company_name"),
                        "reason": "duplicate_company_id",
                    }
                )
                continue

            if normalized_name and normalized_name in seen_names:
                kept = seen_names[normalized_name]
                duplicates.append(
                    {
                        "source_file": path.name,
                        "source_row": row_number,
                        "skipped_company_id": company_id,
                        "skipped_company_name": company_name or None,
                        "kept_company_id": kept.get("company_id"),
                        "kept_company_name": kept.get("company_name"),
                        "reason": "duplicate_exact_company_name",
                    }
                )
                continue

            seen_ids[company_id] = row
            if normalized_name:
                seen_names[normalized_name] = row
            merged.append(row)

    return merged, sources, duplicates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="company_seed_1000.json")
    parser.add_argument("--expansion-glob", default="company_seed_expansion_*.json")
    parser.add_argument("--merged-seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--duplicate-report-file", default="runtime/company_seed_duplicates.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--start-index", type=int)
    args = parser.parse_args()

    base_path = Path(args.seed_file)
    merged, sources, duplicates = merge_seeds(base_path, args.expansion_glob)
    merged_path = Path(args.merged_seed_file)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "target_count": 1000,
                "seed_status": "incrementally_expanded_auditable_seed",
                "valid_seed_count": len(merged),
                "duplicate_seed_rows_excluded": len(duplicates),
                "source_files": sources,
                "companies": merged,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    duplicate_path = Path(args.duplicate_report_file)
    duplicate_path.parent.mkdir(parents=True, exist_ok=True)
    duplicate_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "duplicate_seed_rows_excluded": len(duplicates),
                "duplicates": duplicates,
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
        "--max-workers",
        str(args.max_workers),
    ]
    if args.start_index is not None:
        cmd.extend(["--start-index", str(args.start_index)])
    print(
        json.dumps(
            {
                "merged_seed_count": len(merged),
                "duplicate_seed_rows_excluded": len(duplicates),
                "sources": sources,
            },
            ensure_ascii=False,
        )
    )
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
