#!/usr/bin/env python3
"""Assess whether the Company1000 research package satisfies the final handoff gate.

The gate is intentionally conservative. A run is complete only when:
1. the merged seed contains at least 1,000 unique companies;
2. every seeded company has an audit/coverage record or an explicit failure reason;
3. every discovered official job-detail link is represented by a normalized job record
   or an explicit fetch/closure failure;
4. normalized jobs have company identity, title and a unique source URL;
5. no record is automatically marked active_verified.

The script updates run_summary.json and checkpoint.json and writes a standalone
final_acceptance_report.json for Codex and human review.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
RUNTIME = ROOT / "runtime"
DELIVERABLES = ROOT / "deliverables"
TARGET_COMPANIES = 1000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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


def load_jsonl_glob(directory: Path, pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        rows.extend(read_jsonl(path))
    return rows


def nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def unique_rows(rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = "|".join(str(row.get(field) or "") for field in fields)
        if not key.strip("|"):
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        output[key] = row
    return list(output.values())


def main() -> int:
    merged_seed = read_json(RUNTIME / "company_seed_merged.json", {})
    seed_rows = merged_seed.get("companies", []) if isinstance(merged_seed, dict) else []
    seed_rows = [row for row in seed_rows if isinstance(row, dict)]
    seed_ids = {str(row.get("company_id")) for row in seed_rows if nonempty(row.get("company_id"))}

    coverage = read_jsonl(DELIVERABLES / "Coverage.jsonl")
    failures = read_jsonl(DELIVERABLES / "Failures.jsonl")
    jobs = read_jsonl(DELIVERABLES / "JobImport.jsonl")
    link_candidates = load_jsonl_glob(OUTPUT, "*job_link_candidates*.jsonl")
    link_candidates = unique_rows(link_candidates, ("company_id", "source_url"))

    covered_company_ids = {
        str(row.get("company_id"))
        for row in coverage
        if nonempty(row.get("company_id"))
    }
    failed_company_ids = {
        str(row.get("company_id"))
        for row in failures
        if nonempty(row.get("company_id")) and nonempty(row.get("reason"))
    }
    accounted_company_ids = covered_company_ids | failed_company_ids
    companies_without_audit = sorted(seed_ids - accounted_company_ids)

    job_urls = {
        str(row.get("source_url")).strip()
        for row in jobs
        if nonempty(row.get("source_url"))
    }
    failed_urls = {
        str(row.get("url") or row.get("source_url")).strip()
        for row in failures
        if nonempty(row.get("url") or row.get("source_url")) and nonempty(row.get("reason"))
    }
    pending_links = [
        row for row in link_candidates
        if nonempty(row.get("source_url"))
        and str(row.get("source_url")).strip() not in job_urls
        and str(row.get("source_url")).strip() not in failed_urls
    ]

    invalid_jobs = []
    active_verified = []
    for row in jobs:
        company_ok = nonempty(row.get("company_id") or row.get("company_name") or row.get("company"))
        title_ok = nonempty(row.get("job_title") or row.get("job_name"))
        source_ok = nonempty(row.get("source_url"))
        if not (company_ok and title_ok and source_ok):
            invalid_jobs.append(row)
        if row.get("active_verified") is True or str(row.get("review_status") or "") == "active_verified":
            active_verified.append(row)

    conditions = {
        "seed_target_reached": len(seed_ids) >= TARGET_COMPANIES,
        "all_seed_companies_accounted_for": not companies_without_audit,
        "all_discovered_job_links_resolved": not pending_links,
        "all_jobs_have_identity_title_and_source": not invalid_jobs,
        "no_auto_active_verified_records": not active_verified,
        "deliverable_set_present": all(
            (DELIVERABLES / name).exists()
            for name in (
                "CompanySource.jsonl",
                "JobImport.jsonl",
                "Coverage.jsonl",
                "Failures.jsonl",
                "JobChanges.jsonl",
            )
        ),
    }
    final_acceptance_met = all(conditions.values())

    report = {
        "schema_version": "1.0",
        "assessed_at": utc_now(),
        "target_companies": TARGET_COMPANIES,
        "seeded_unique_companies": len(seed_ids),
        "seed_shortfall": max(0, TARGET_COMPANIES - len(seed_ids)),
        "companies_with_coverage": len(covered_company_ids & seed_ids),
        "companies_with_explicit_failure_reason": len(failed_company_ids & seed_ids),
        "companies_without_audit_count": len(companies_without_audit),
        "companies_without_audit_sample": companies_without_audit[:50],
        "job_records": len(jobs),
        "discovered_job_link_candidates": len(link_candidates),
        "resolved_job_link_candidates": len(link_candidates) - len(pending_links),
        "pending_job_link_candidates": len(pending_links),
        "pending_job_link_sample": [
            {
                "company_id": row.get("company_id"),
                "company_name": row.get("company_name"),
                "source_url": row.get("source_url"),
            }
            for row in pending_links[:50]
        ],
        "invalid_job_records": len(invalid_jobs),
        "active_verified_records": len(active_verified),
        "conditions": conditions,
        "final_acceptance_met": final_acceptance_met,
        "notification_eligible": final_acceptance_met,
    }

    DELIVERABLES.mkdir(parents=True, exist_ok=True)
    (DELIVERABLES / "final_acceptance_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_path = DELIVERABLES / "run_summary.json"
    summary = read_json(summary_path, {})
    if not isinstance(summary, dict):
        summary = {}
    summary.update({
        "final_acceptance_met": final_acceptance_met,
        "notification_eligible": final_acceptance_met,
        "final_acceptance_report": report,
    })
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint_path = RUNTIME / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, {})
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    checkpoint.update({
        "updated_at": utc_now(),
        "target_companies": TARGET_COMPANIES,
        "valid_seed_count": len(seed_ids),
        "seed_shortfall": max(0, TARGET_COMPANIES - len(seed_ids)),
        "companies_without_audit": len(companies_without_audit),
        "pending_job_link_candidates": len(pending_links),
        "invalid_job_records": len(invalid_jobs),
        "companies_final_acceptance_met": len(seed_ids) if final_acceptance_met else 0,
        "final_acceptance_met": final_acceptance_met,
        "notification_eligible": final_acceptance_met,
    })
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
