#!/usr/bin/env python3
"""Merge the base company seed with incremental expansion files, then audit pending entries.

The merge is conservative and auditable:
- exact duplicate legal-entity names are excluded;
- repeated IDs for the same company are excluded;
- repeated IDs assigned to different companies are preserved by deterministic ID repair;
- every exclusion or repair is written to a seed-integrity report.
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


def source_suffix(path: Path) -> str:
    match = re.search(r"(\d{4,})", path.stem)
    return f"e{match.group(1)}" if match else re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")


def repaired_company_id(original_id: str, path: Path, seen_ids: dict[str, dict[str, Any]]) -> str:
    base = f"{original_id}-{source_suffix(path)}"
    candidate = base
    sequence = 2
    while candidate in seen_ids:
        candidate = f"{base}-{sequence}"
        sequence += 1
    return candidate


def merge_seeds(
    base_path: Path,
    expansion_glob: str,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    seen_ids: dict[str, dict[str, Any]] = {}
    seen_names: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    duplicates: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []
    paths = [base_path, *sorted(base_path.parent.glob(expansion_glob))]

    for path in paths:
        if not path.exists():
            continue
        sources.append(path.name)
        for row_number, source_row in enumerate(load_seed(path), start=1):
            row = dict(source_row)
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

            if company_id in seen_ids:
                kept = seen_ids[company_id]
                kept_name = normalize_company_name(kept.get("company_name"))
                if normalized_name and normalized_name == kept_name:
                    duplicates.append(
                        {
                            "source_file": path.name,
                            "source_row": row_number,
                            "skipped_company_id": company_id,
                            "skipped_company_name": company_name or None,
                            "kept_company_id": kept.get("company_id"),
                            "kept_company_name": kept.get("company_name"),
                            "reason": "duplicate_company_id_same_company",
                        }
                    )
                    continue

                repaired_id = repaired_company_id(company_id, path, seen_ids)
                collisions.append(
                    {
                        "source_file": path.name,
                        "source_row": row_number,
                        "original_company_id": company_id,
                        "repaired_company_id": repaired_id,
                        "company_name": company_name or None,
                        "collided_with_company_name": kept.get("company_name"),
                        "reason": "company_id_collision_different_company",
                    }
                )
                row["source_company_id"] = company_id
                row["company_id"] = repaired_id
                row["company_id_repair_reason"] = "collision_with_different_company"
                company_id = repaired_id

            seen_ids[company_id] = row
            if normalized_name:
                seen_names[normalized_name] = row
            merged.append(row)

    return merged, sources, duplicates, collisions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="company_seed_1000.json")
    parser.add_argument("--expansion-glob", default="company_seed_expansion_*.json")
    parser.add_argument("--merged-seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--integrity-report-file", default="runtime/company_seed_duplicates.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--start-index", type=int)
    args = parser.parse_args()

    base_path = Path(args.seed_file)
    merged, sources, duplicates, collisions = merge_seeds(base_path, args.expansion_glob)
    merged_path = Path(args.merged_seed_file)
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_text(
        json.dumps(
            {
                "schema_version": "1.2",
                "target_count": 1000,
                "seed_status": "incrementally_expanded_auditable_seed",
                "valid_seed_count": len(merged),
                "duplicate_seed_rows_excluded": len(duplicates),
                "company_id_collisions_resolved": len(collisions),
                "source_files": sources,
                "companies": merged,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    integrity_path = Path(args.integrity_report_file)
    integrity_path.parent.mkdir(parents=True, exist_ok=True)
    integrity_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "duplicate_seed_rows_excluded": len(duplicates),
                "company_id_collisions_resolved": len(collisions),
                "duplicates": duplicates,
                "id_collisions": collisions,
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
                "company_id_collisions_resolved": len(collisions),
                "sources": sources,
            },
            ensure_ascii=False,
        )
    )
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
