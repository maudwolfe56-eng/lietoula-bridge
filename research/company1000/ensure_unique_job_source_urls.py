#!/usr/bin/env python3
"""Ensure each import row has a stable unique source identifier.

An official recruitment announcement can legitimately contain several positions on one
public page. The first record keeps the canonical page URL so candidate resolution still
matches. Additional records retain the official page in ``source_page_url`` and append a
stable record fragment to ``source_url``. The fragment is an import identity only; it does
not claim that the employer published a separate page.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
JOB_FILE = ROOT / "deliverables" / "JobImport.jsonl"


def read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def stable_token(row: dict, index: int) -> str:
    value = row.get("record_id") or row.get("source_record_key") or row.get("job_title") or index
    return quote(str(value), safe="-_.~")


def main() -> int:
    rows = read_rows(JOB_FILE)
    groups: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        url = str(row.get("source_url") or "").strip()
        if url:
            groups[url].append(index)

    changed = 0
    duplicate_groups = 0
    for canonical_url, indexes in groups.items():
        if len(indexes) <= 1:
            continue
        duplicate_groups += 1
        for ordinal, row_index in enumerate(indexes[1:], start=2):
            row = rows[row_index]
            row["source_page_url"] = canonical_url
            separator = "&" if "#" in canonical_url else "#"
            token = stable_token(row, ordinal)
            row["source_url"] = f"{canonical_url}{separator}job={token}"
            row["source_url_uniqueness_strategy"] = "official_page_plus_record_fragment"
            changed += 1

    if rows:
        JOB_FILE.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    print(json.dumps({
        "job_records": len(rows),
        "duplicate_source_url_groups": duplicate_groups,
        "records_given_unique_fragments": changed,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
