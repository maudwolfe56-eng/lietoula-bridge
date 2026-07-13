#!/usr/bin/env python3
"""Assess whether the Company1000 package satisfies the final handoff gate.

The gate is intentionally conservative. A run is complete only when:
1. the merged seed contains at least 1,000 unique companies;
2. every seeded company has an audit/coverage record or an explicit failure reason;
3. every probable official job-detail link is represented by a normalized job record
   or a terminal, explicit access/closure failure;
4. every recruitment navigation page has a site-specific enumeration resolution;
5. normalized jobs have company identity, title and a unique source URL;
6. no record is automatically marked active_verified.

Transient fetch errors and pages that are merely unparseable are not terminal. They
remain pending so the package cannot pass by silently discarding public openings.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from crawl_company_jobs import is_probable_job_detail_url

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
RUNTIME = ROOT / "runtime"
DELIVERABLES = ROOT / "deliverables"
TARGET_COMPANIES = 1000
NAVIGATION_RESOLUTION_STATUSES = {
    "enumerated",
    "no_current_openings_observed",
    "superseded",
    "not_job_navigation",
    "restricted_with_explicit_reason",
}
TERMINAL_DETAIL_FAILURE_REASONS = {
    "detail_access_restricted",
    "detail_closed_or_removed",
    "detail_not_found",
    "detail_gone",
}
GENERIC_NUMBERED_CATEGORY = re.compile(
    r"/(?:job|jobs|career|careers|recruitment)-\d{1,4}\.(?:s?html?|aspx?)$",
    re.I,
)


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


def looks_like_numbered_category(url: str) -> bool:
    return bool(GENERIC_NUMBERED_CATEGORY.search(urlparse(url).path))


def candidate_kind(row: dict[str, Any]) -> str:
    source_type = str(row.get("source_type") or "")
    url = str(row.get("source_url") or "").strip()
    if source_type == "official_recruitment_navigation":
        return "recruitment_navigation"
    if looks_like_numbered_category(url):
        return "recruitment_navigation"
    if source_type == "official_job_detail_candidate":
        return "job_detail" if is_probable_job_detail_url(url) else "recruitment_navigation"
    return "job_detail" if is_probable_job_detail_url(url) else "recruitment_navigation"


def terminal_detail_failure(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason") or "")
    status = row.get("http_status")
    if reason in TERMINAL_DETAIL_FAILURE_REASONS:
        return True
    if reason == "detail_http_error" and status in {404, 410}:
        return True
    if reason.startswith("detail_access_restricted"):
        return True
    return False


def sample_rows(rows: list[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "source_url": row.get("source_url") or row.get("url"),
            "reason": row.get("reason") or row.get("resolution_status"),
        }
        for row in rows[:limit]
    ]


def main() -> int:
    merged_seed = read_json(RUNTIME / "company_seed_merged.json", {})
    seed_rows = merged_seed.get("companies", []) if isinstance(merged_seed, dict) else []
    seed_rows = [row for row in seed_rows if isinstance(row, dict)]
    seed_ids = {str(row.get("company_id")) for row in seed_rows if nonempty(row.get("company_id"))}

    coverage = read_jsonl(DELIVERABLES / "Coverage.jsonl")
    failures = read_jsonl(DELIVERABLES / "Failures.jsonl")
    jobs = read_jsonl(DELIVERABLES / "JobImport.jsonl")

    legacy_candidates = load_jsonl_glob(OUTPUT, "*job_link_candidates*.jsonl")
    explicit_navigation = load_jsonl_glob(OUTPUT, "*recruitment_navigation*.jsonl")
    all_candidates = unique_rows(
        [*legacy_candidates, *explicit_navigation],
        ("company_id", "source_url"),
    )
    detail_candidates = [row for row in all_candidates if candidate_kind(row) == "job_detail"]
    navigation_candidates = [row for row in all_candidates if candidate_kind(row) == "recruitment_navigation"]

    navigation_resolutions = load_jsonl_glob(OUTPUT, "*recruitment_navigation_resolution*.jsonl")
    resolved_navigation_urls = {
        str(row.get("source_url") or row.get("url")).strip()
        for row in navigation_resolutions
        if nonempty(row.get("source_url") or row.get("url"))
        and str(row.get("resolution_status") or "") in NAVIGATION_RESOLUTION_STATUSES
    }

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
    terminal_failed_urls = {
        str(row.get("url") or row.get("source_url")).strip()
        for row in failures
        if nonempty(row.get("url") or row.get("source_url")) and terminal_detail_failure(row)
    }
    pending_detail_links = [
        row for row in detail_candidates
        if nonempty(row.get("source_url"))
        and str(row.get("source_url")).strip() not in job_urls
        and str(row.get("source_url")).strip() not in terminal_failed_urls
    ]
    pending_navigation = [
        row for row in navigation_candidates
        if nonempty(row.get("source_url"))
        and str(row.get("source_url")).strip() not in resolved_navigation_urls
    ]

    invalid_jobs = []
    active_verified = []
    duplicate_job_urls: dict[str, int] = {}
    for row in jobs:
        company_ok = nonempty(row.get("company_id") or row.get("company_name") or row.get("company"))
        title_ok = nonempty(row.get("job_title") or row.get("job_name"))
        source_ok = nonempty(row.get("source_url"))
        if not (company_ok and title_ok and source_ok):
            invalid_jobs.append(row)
        if source_ok:
            source_url = str(row.get("source_url")).strip()
            duplicate_job_urls[source_url] = duplicate_job_urls.get(source_url, 0) + 1
        if row.get("active_verified") is True or str(row.get("review_status") or "") == "active_verified":
            active_verified.append(row)
    duplicate_job_url_count = sum(1 for count in duplicate_job_urls.values() if count > 1)

    conditions = {
        "seed_target_reached": len(seed_ids) >= TARGET_COMPANIES,
        "all_seed_companies_accounted_for": not companies_without_audit,
        "all_discovered_job_detail_links_resolved": not pending_detail_links,
        "all_recruitment_navigation_enumerated": not pending_navigation,
        "all_jobs_have_identity_title_and_source": not invalid_jobs,
        "job_source_urls_unique": duplicate_job_url_count == 0,
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
        "schema_version": "1.2",
        "assessed_at": utc_now(),
        "target_companies": TARGET_COMPANIES,
        "seeded_unique_companies": len(seed_ids),
        "seed_shortfall": max(0, TARGET_COMPANIES - len(seed_ids)),
        "companies_with_coverage": len(covered_company_ids & seed_ids),
        "companies_with_explicit_failure_reason": len(failed_company_ids & seed_ids),
        "companies_without_audit_count": len(companies_without_audit),
        "companies_without_audit_sample": companies_without_audit[:50],
        "job_records": len(jobs),
        "all_recruitment_candidates": len(all_candidates),
        "probable_job_detail_candidates": len(detail_candidates),
        "terminal_detail_failures": len(terminal_failed_urls),
        "resolved_job_detail_candidates": len(detail_candidates) - len(pending_detail_links),
        "pending_job_detail_candidates": len(pending_detail_links),
        "pending_job_detail_sample": sample_rows(pending_detail_links),
        "recruitment_navigation_candidates": len(navigation_candidates),
        "resolved_recruitment_navigation": len(navigation_candidates) - len(pending_navigation),
        "pending_recruitment_navigation": len(pending_navigation),
        "pending_recruitment_navigation_sample": sample_rows(pending_navigation),
        "invalid_job_records": len(invalid_jobs),
        "duplicate_job_source_urls": duplicate_job_url_count,
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
        "pending_job_link_candidates": len(pending_detail_links),
        "pending_recruitment_navigation": len(pending_navigation),
        "invalid_job_records": len(invalid_jobs),
        "duplicate_job_source_urls": duplicate_job_url_count,
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
