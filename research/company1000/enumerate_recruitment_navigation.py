#!/usr/bin/env python3
"""Expand public recruitment-navigation pages without bypassing access controls.

The generic company auditor intentionally separates recruitment home/list pages from
probable job-detail URLs. This script follows those public navigation links one level
at a time, records child navigation and raw detail candidates, and emits an explicit
resolution ledger. It also reclassifies historical generic links that predate the
navigation/detail split. It never logs in, solves CAPTCHA, infers salary, or promotes
a job beyond ``candidate_raw``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from crawl_company_jobs import (
    USER_AGENT,
    append_jsonl,
    fetch_page,
    is_probable_job_detail_url,
    load_companies,
    utc_now,
)

ACCEPTED_RESOLUTION_STATUSES = {
    "enumerated",
    "no_current_openings_observed",
    "superseded",
    "not_job_navigation",
    "restricted_with_explicit_reason",
}


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


def load_glob(directory: Path, pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        if "resolution" in path.name:
            continue
        rows.extend(read_jsonl(path))
    return rows


def nav_key(company_id: str, source_url: str) -> str:
    return hashlib.sha256(f"{company_id}|{source_url}".encode()).hexdigest()[:20]


def canonical_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        company_id = str(row.get("company_id") or "").strip()
        source_url = str(row.get("source_url") or row.get("url") or "").strip()
        if not company_id or not source_url.startswith(("http://", "https://")):
            continue
        key = nav_key(company_id, source_url)
        normalized = dict(row)
        normalized["company_id"] = company_id
        normalized["source_url"] = source_url
        normalized["source_type"] = "official_recruitment_navigation"
        normalized["navigation_key"] = key
        unique[key] = normalized
    return list(unique.values())


def historical_navigation_candidates(output_dir: Path) -> list[dict[str, Any]]:
    """Recover generic career/list URLs written before the detail classifier existed."""
    rows: list[dict[str, Any]] = []
    for row in load_glob(output_dir, "*job_link_candidates*.jsonl"):
        source_url = str(row.get("source_url") or "").strip()
        source_type = str(row.get("source_type") or "")
        if source_type == "official_job_detail_candidate":
            continue
        if source_type == "official_recruitment_navigation" or not is_probable_job_detail_url(source_url):
            normalized = dict(row)
            normalized["source_type"] = "official_recruitment_navigation"
            normalized["enumeration_status"] = "historical_candidate_reclassified"
            rows.append(normalized)
    return rows


def audit_navigation(
    row: dict[str, Any],
    company_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    company_id = str(row.get("company_id") or "")
    company = company_by_id.get(company_id, {})
    company_name = str(row.get("company_name") or company.get("company_name") or "")
    source_url = str(row.get("source_url") or "")
    official_site = str(company.get("official_website") or source_url)
    career_url = str(company.get("career_url") or source_url)
    observed_at = utc_now()

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })
    result = fetch_page(session, source_url, official_site, career_url)

    child_navigation = [url for url in result.recruitment_links if url != source_url and url not in result.job_links]
    detail_links = [url for url in result.job_links if url != source_url]

    if result.http_status in {404, 410}:
        resolution_status = "superseded"
        reason = f"http_{result.http_status}"
    elif result.blocked or result.error:
        resolution_status = "restricted_with_explicit_reason"
        reason = result.error or f"http_{result.http_status}"
    elif result.javascript_shell:
        resolution_status = "requires_site_specific_dynamic_enumerator"
        reason = "javascript_shell_no_public_html_job_enumeration"
    elif detail_links or child_navigation:
        resolution_status = "enumerated"
        reason = None
    else:
        resolution_status = "requires_manual_review_no_links"
        reason = "public_page_reached_but_no_enumerable_links_observed"

    resolution = {
        "navigation_key": nav_key(company_id, source_url),
        "company_id": company_id,
        "company_name": company_name,
        "source_url": source_url,
        "final_url": result.final_url,
        "http_status": result.http_status,
        "page_title": result.title,
        "text_length": result.text_length,
        "resolution_status": resolution_status,
        "reason": reason,
        "job_detail_candidates_discovered": len(detail_links),
        "child_navigation_discovered": len(child_navigation),
        "observed_at": observed_at,
    }
    details = [{
        "company_id": company_id,
        "company_name": company_name,
        "source_url": url,
        "source_type": "official_job_detail_candidate",
        "review_status": "candidate_raw",
        "promotion_eligible": False,
        "review_recommendation": "fetch_detail_and_validate_required_fields",
        "discovered_via": source_url,
        "discovered_at": observed_at,
    } for url in detail_links]
    children = [{
        "company_id": company_id,
        "company_name": company_name,
        "source_url": url,
        "source_type": "official_recruitment_navigation",
        "enumeration_status": "requires_navigation_expansion",
        "parent_navigation_url": source_url,
        "discovered_at": observed_at,
    } for url in child_navigation]
    failures: list[dict[str, Any]] = []
    if resolution_status not in {"enumerated", "superseded"}:
        failures.append({
            "company_id": company_id,
            "company_name": company_name,
            "url": source_url,
            "source_url": source_url,
            "reason": resolution_status,
            "detail": reason,
            "http_status": result.http_status,
            "observed_at": observed_at,
        })
    return resolution, details, children, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()

    seed_rows = load_companies(Path(args.seed_file))
    company_by_id = {str(row.get("company_id") or ""): row for row in seed_rows}
    output_dir = Path(args.output_dir)
    state_path = Path(args.state_file)

    explicit_navigation = load_glob(output_dir, "*recruitment_navigation*.jsonl")
    legacy_navigation = historical_navigation_candidates(output_dir)
    candidates = canonical_candidates([*explicit_navigation, *legacy_navigation])
    resolution_file = output_dir / "recruitment_navigation_resolution_auto.jsonl"
    prior_resolutions = read_jsonl(resolution_file)
    accepted = {
        nav_key(str(row.get("company_id") or ""), str(row.get("source_url") or row.get("url") or ""))
        for row in prior_resolutions
        if str(row.get("resolution_status") or "") in ACCEPTED_RESOLUTION_STATUSES
    }
    attempts = Counter(str(row.get("navigation_key") or nav_key(
        str(row.get("company_id") or ""), str(row.get("source_url") or row.get("url") or "")
    )) for row in prior_resolutions)

    pending = [
        row for row in candidates
        if row["navigation_key"] not in accepted
        and attempts[row["navigation_key"]] < max(1, args.max_attempts)
    ][: max(1, args.batch_size)]

    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(lambda row: audit_navigation(row, company_by_id), pending))

    resolutions: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for resolution, detail_rows, child_rows, failure_rows in results:
        resolutions.append(resolution)
        details.extend(detail_rows)
        children.extend(child_rows)
        failures.extend(failure_rows)

    append_jsonl(resolution_file, resolutions)
    append_jsonl(output_dir / "job_link_candidates_from_navigation.jsonl", details)
    append_jsonl(output_dir / "recruitment_navigation_expanded.jsonl", children)
    append_jsonl(output_dir / "failures_navigation.jsonl", failures)

    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    status_counts = Counter(str(row.get("resolution_status") or "unknown") for row in resolutions)
    state.update({
        "updated_at": utc_now(),
        "navigation_expansion_last_batch": {
            "candidate_count": len(candidates),
            "legacy_candidates_reclassified": len(legacy_navigation),
            "selected_count": len(pending),
            "processed_count": len(resolutions),
            "detail_candidates_discovered": len(details),
            "child_navigation_discovered": len(children),
            "resolution_status_counts": dict(status_counts),
            "max_workers": worker_count,
        },
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(state.get("navigation_expansion_last_batch", {}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
