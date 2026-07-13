#!/usr/bin/env python3
"""Materialize conservative coverage placeholders for seeded companies missing from ledgers.

This repairs reporting gaps caused by interrupted/concurrent historical runs without
pretending that an official career entry or its jobs were successfully verified.
Placeholders remain retryable and never satisfy final acceptance.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
RUNTIME = ROOT / "runtime"


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
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def audit_key(company_id: str, career_url: str) -> str:
    return hashlib.sha256(f"{company_id}|{career_url}".encode()).hexdigest()[:20]


def main() -> int:
    merged_path = RUNTIME / "company_seed_merged.json"
    if not merged_path.exists():
        raise SystemExit("missing runtime/company_seed_merged.json")
    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    companies = [row for row in merged.get("companies", []) if isinstance(row, dict)]

    observed_company_ids: set[str] = set()
    for path in sorted(OUTPUT.glob("coverage*.jsonl")):
        if path.name == "coverage_reconciled.jsonl":
            continue
        for row in read_jsonl(path):
            company_id = str(row.get("company_id") or "").strip()
            if company_id:
                observed_company_ids.add(company_id)

    generated_at = utc_now()
    coverage_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    for company in companies:
        company_id = str(company.get("company_id") or "").strip()
        if not company_id or company_id in observed_company_ids:
            continue
        name = str(company.get("company_name") or company.get("short_name") or "")
        career_url = str(company.get("career_url") or "").strip()
        official_site = str(company.get("official_website") or "").strip()
        reason = "audit_ledger_gap_retry_required"
        coverage_rows.append({
            "audit_key": audit_key(company_id, career_url),
            "company_id": company_id,
            "company_name": name,
            "official_entry_url": career_url or None,
            "official_website": official_site or None,
            "final_url": None,
            "official_entry_verified": False,
            "http_status": None,
            "page_title": None,
            "text_length": 0,
            "enumeration_status": reason,
            "job_link_candidate_count": 0,
            "error": reason,
            "observed_at": generated_at,
            "final_acceptance_met": False,
            "reconciliation_placeholder": True,
            "retry_required": True,
        })
        failure_rows.append({
            "company_id": company_id,
            "company_name": name,
            "url": career_url or official_site or None,
            "reason": reason,
            "observed_at": generated_at,
            "retry_required": True,
        })

    write_jsonl(OUTPUT / "coverage_reconciled.jsonl", coverage_rows)
    write_jsonl(OUTPUT / "failures_reconciled.jsonl", failure_rows)
    print(json.dumps({
        "seeded_companies": len(companies),
        "companies_observed_in_raw_ledgers": len(observed_company_ids),
        "coverage_placeholders_written": len(coverage_rows),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
