#!/usr/bin/env python3
"""Capture and compare auditable JobImport snapshots.

Usage:
  python detect_job_changes.py capture
  python detect_job_changes.py detect

The detector preserves prior JobChanges history and appends stable new/modified/status-transition
records. It does not alter JobImport review states and never creates active_verified records.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DELIVERABLES = ROOT / "deliverables"
RUNTIME = ROOT / "runtime"
CURRENT = DELIVERABLES / "JobImport.jsonl"
BEFORE = RUNTIME / "job_snapshot_before_run.jsonl"
CHANGES_JSONL = DELIVERABLES / "JobChanges.jsonl"
CHANGES_CSV = DELIVERABLES / "JobChanges.csv"
CHANGES_BEFORE = RUNTIME / "job_changes_before_run.jsonl"

MATERIAL_FIELDS = (
    "company_id",
    "company_name",
    "job_title",
    "job_name",
    "department",
    "city",
    "location",
    "salary",
    "salary_disclosure_status",
    "responsibilities",
    "requirements",
    "experience",
    "education",
    "published_date",
    "deadline_date",
    "review_status",
    "review_recommendation",
    "source_type",
    "source_url",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            })


def identity(row: dict[str, Any]) -> str:
    record_id = str(row.get("record_id") or "").strip()
    if record_id:
        return f"record:{record_id}"
    source_url = str(row.get("source_url") or "").strip()
    if source_url:
        return f"url:{source_url}"
    fallback = "|".join(str(row.get(field) or "") for field in (
        "company_id", "job_title", "department", "city", "published_date"
    ))
    return "fallback:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def material(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in MATERIAL_FIELDS}


def fingerprint(row: dict[str, Any]) -> str:
    payload = json.dumps(material(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inactive(status: Any) -> bool:
    return str(status or "").startswith("inactive_")


def capture() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if CURRENT.exists():
        shutil.copyfile(CURRENT, BEFORE)
        job_count = len(read_jsonl(BEFORE))
    else:
        BEFORE.write_text("", encoding="utf-8")
        job_count = 0

    if CHANGES_JSONL.exists():
        shutil.copyfile(CHANGES_JSONL, CHANGES_BEFORE)
        change_count = len(read_jsonl(CHANGES_BEFORE))
    else:
        CHANGES_BEFORE.write_text("", encoding="utf-8")
        change_count = 0

    print(json.dumps({
        "captured_previous_jobs": job_count,
        "captured_change_history": change_count,
        "job_snapshot": str(BEFORE),
        "change_snapshot": str(CHANGES_BEFORE),
    }, ensure_ascii=False))
    return 0


def change_record(
    change_type: str,
    current: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    changed_fields: list[str],
) -> dict[str, Any]:
    representative = current or previous or {}
    previous_fp = fingerprint(previous) if previous else None
    current_fp = fingerprint(current) if current else None
    return {
        "record_id": representative.get("record_id"),
        "company_id": representative.get("company_id"),
        "company_name": representative.get("company_name") or representative.get("company"),
        "job_title": representative.get("job_title") or representative.get("job_name"),
        "source_url": representative.get("source_url"),
        "change_type": change_type,
        "changed_fields": changed_fields,
        "previous_review_status": previous.get("review_status") if previous else None,
        "current_review_status": current.get("review_status") if current else None,
        "previous_fingerprint": previous_fp,
        "current_fingerprint": current_fp,
        "detected_at": utc_now(),
        "active_verified": False,
    }


def detect() -> int:
    previous_rows = read_jsonl(BEFORE)
    current_rows = read_jsonl(CURRENT)
    history = read_jsonl(CHANGES_BEFORE) if CHANGES_BEFORE.exists() else read_jsonl(CHANGES_JSONL)

    previous = {identity(row): row for row in previous_rows}
    current = {identity(row): row for row in current_rows}
    detected: list[dict[str, Any]] = []

    for key, row in current.items():
        old = previous.get(key)
        if old is None:
            detected.append(change_record("new", row, None, list(MATERIAL_FIELDS)))
            continue
        changed = [field for field in MATERIAL_FIELDS if old.get(field) != row.get(field)]
        if not changed:
            continue
        if not inactive(old.get("review_status")) and inactive(row.get("review_status")):
            change_type = "closed_or_expired"
        elif inactive(old.get("review_status")) and not inactive(row.get("review_status")):
            change_type = "reopened"
        else:
            change_type = "modified"
        detected.append(change_record(change_type, row, old, changed))

    for key, old in previous.items():
        if key not in current:
            detected.append(change_record("removed_from_current_snapshot", None, old, list(MATERIAL_FIELDS)))

    merged = list(history)
    seen = {
        (
            row.get("record_id"), row.get("source_url"), row.get("change_type"),
            row.get("previous_fingerprint"), row.get("current_fingerprint"),
        )
        for row in merged
    }
    appended = 0
    for row in detected:
        dedup_key = (
            row.get("record_id"), row.get("source_url"), row.get("change_type"),
            row.get("previous_fingerprint"), row.get("current_fingerprint"),
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        merged.append(row)
        appended += 1

    write_jsonl(CHANGES_JSONL, merged)
    write_csv(CHANGES_CSV, merged)
    print(json.dumps({
        "previous_jobs": len(previous_rows),
        "current_jobs": len(current_rows),
        "prior_change_history": len(history),
        "detected_changes": len(detected),
        "appended_changes": appended,
        "total_change_history": len(merged),
        "active_verified_created": 0,
    }, ensure_ascii=False))
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"capture", "detect"}:
        print("usage: detect_job_changes.py capture|detect", file=sys.stderr)
        return 2
    return capture() if sys.argv[1] == "capture" else detect()


if __name__ == "__main__":
    raise SystemExit(main())
