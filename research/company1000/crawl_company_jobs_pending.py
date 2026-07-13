#!/usr/bin/env python3
"""Audit the next truly unprocessed company entries.

This wrapper scans from the checkpoint cursor, wraps once, and selects only
companies whose current company_id/career_url audit key is absent from the
coverage ledger. It preserves the conservative policy of crawl_company_jobs.py
and uses bounded parallelism only for independent public HTTP requests.

A discovered link is only a raw candidate. Recruitment navigation is stored
separately from probable job details; neither is promoted to review_pending
without fetching and validating the detail record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from crawl_company_jobs import (
    JOB_WORDS,
    USER_AGENT,
    append_jsonl,
    existing_keys,
    fetch_page,
    host,
    load_companies,
    registrable_hint,
    same_official_family,
    utc_now,
)


def company_audit_key(company: dict[str, Any]) -> str:
    company_id = str(company.get("company_id") or "")
    career_url = str(company.get("career_url") or "")
    return hashlib.sha256(f"{company_id}|{career_url}".encode()).hexdigest()[:20]


def select_pending(
    companies: list[dict[str, Any]],
    processed: set[str],
    start: int,
    batch_size: int,
) -> list[tuple[int, dict[str, Any]]]:
    if not companies:
        return []
    start = max(0, min(start, len(companies)))
    indexes = list(range(start, len(companies))) + list(range(0, start))
    selected: list[tuple[int, dict[str, Any]]] = []
    for index in indexes:
        company = companies[index]
        if company_audit_key(company) in processed:
            continue
        selected.append((index, company))
        if len(selected) >= max(1, batch_size):
            break
    return selected


def audit_one(
    entry: tuple[int, dict[str, Any]],
) -> tuple[
    int,
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    index, company = entry
    company_id = str(company.get("company_id") or "")
    name = str(company.get("company_name") or company.get("name") or "")
    career_url = str(company.get("career_url") or "")
    official_site = str(company.get("official_website") or career_url)
    seed_status = str(company.get("seed_status") or "")
    audit_key = company_audit_key(company)
    observed_at = utc_now()

    if not career_url:
        coverage = {
            "audit_key": audit_key,
            "company_id": company_id,
            "company_name": name,
            "official_entry_url": None,
            "final_url": None,
            "official_entry_verified": False,
            "http_status": None,
            "page_title": None,
            "text_length": 0,
            "enumeration_status": "missing_career_url",
            "job_link_candidate_count": 0,
            "recruitment_navigation_count": 0,
            "error": "missing_career_url",
            "observed_at": observed_at,
            "final_acceptance_met": False,
        }
        failures = [{
            "company_id": company_id,
            "company_name": name,
            "reason": "missing_career_url",
            "observed_at": observed_at,
        }]
        return index, coverage, failures, [], []

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })
    result = fetch_page(session, career_url, official_site, career_url)

    same_registered_family = (
        registrable_hint(host(official_site)) == registrable_hint(host(career_url))
    )
    source_association_supported = same_registered_family or seed_status == "official_entry_verified"
    recruitment_signal_present = bool(result.recruitment_links) or bool(
        JOB_WORDS.search(f"{result.title or ''} {career_url}")
    )
    verified = bool(
        result.http_status
        and 200 <= result.http_status < 400
        and result.final_url
        and same_official_family(result.final_url, official_site, career_url)
        and source_association_supported
        and recruitment_signal_present
    )

    if result.job_links:
        enumeration_status = "job_detail_links_discovered"
    elif result.recruitment_links:
        enumeration_status = "recruitment_navigation_discovered"
    else:
        enumeration_status = "no_links_observed"
    if result.blocked:
        enumeration_status = "restricted"
    elif result.javascript_shell:
        enumeration_status = "pending_dynamic_js"
    elif result.error:
        enumeration_status = "fetch_failed"
    elif not source_association_supported:
        enumeration_status = "official_association_pending_verification"
    elif not recruitment_signal_present:
        enumeration_status = "official_site_reached_no_recruitment_signal"

    observed_at = utc_now()
    coverage = {
        "audit_key": audit_key,
        "company_id": company_id,
        "company_name": name,
        "official_entry_url": career_url,
        "final_url": result.final_url,
        "official_entry_verified": verified,
        "source_association_supported": source_association_supported,
        "recruitment_signal_present": recruitment_signal_present,
        "http_status": result.http_status,
        "page_title": result.title,
        "text_length": result.text_length,
        "enumeration_status": enumeration_status,
        "job_link_candidate_count": len(result.job_links),
        "recruitment_navigation_count": len(result.recruitment_links),
        "error": result.error,
        "observed_at": observed_at,
        "final_acceptance_met": False,
    }
    failures: list[dict[str, Any]] = []
    failure_statuses = {
        "restricted",
        "pending_dynamic_js",
        "fetch_failed",
        "official_association_pending_verification",
        "official_site_reached_no_recruitment_signal",
    }
    if enumeration_status in failure_statuses:
        failures.append({
            "company_id": company_id,
            "company_name": name,
            "url": career_url,
            "reason": enumeration_status,
            "http_status": result.http_status,
            "observed_at": observed_at,
        })
    links = [{
        "company_id": company_id,
        "company_name": name,
        "source_url": job_url,
        "source_type": "official_job_detail_candidate",
        "review_status": "candidate_raw",
        "promotion_eligible": False,
        "review_recommendation": "fetch_detail_and_validate_required_fields",
        "discovered_at": observed_at,
    } for job_url in result.job_links]
    navigation = [{
        "company_id": company_id,
        "company_name": name,
        "source_url": navigation_url,
        "source_type": "official_recruitment_navigation",
        "enumeration_status": "requires_site_specific_enumerator",
        "discovered_at": observed_at,
    } for navigation_url in result.recruitment_links if navigation_url not in result.job_links]
    return index, coverage, failures, links, navigation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--start-index", type=int)
    args = parser.parse_args()

    seed_path = Path(args.seed_file)
    out_dir = Path(args.output_dir)
    state_path = Path(args.state_file)
    companies = load_companies(seed_path)

    state: dict[str, Any] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    start = args.start_index if args.start_index is not None else int(state.get("next_batch_start_index", 0))

    coverage_file = out_dir / "coverage_auto.jsonl"
    failure_file = out_dir / "failures_auto.jsonl"
    link_file = out_dir / "job_link_candidates_auto.jsonl"
    navigation_file = out_dir / "recruitment_navigation_auto.jsonl"
    processed = existing_keys(coverage_file, "audit_key")
    selected = select_pending(companies, processed, start, args.batch_size)

    coverage_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    link_rows: list[dict[str, Any]] = []
    navigation_rows: list[dict[str, Any]] = []

    worker_count = max(1, min(args.max_workers, len(selected) or 1))
    if selected:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(audit_one, selected))
        for _, coverage, failures, links, navigation in sorted(results, key=lambda row: row[0]):
            coverage_rows.append(coverage)
            failure_rows.extend(failures)
            link_rows.extend(links)
            navigation_rows.extend(navigation)

    append_jsonl(coverage_file, coverage_rows)
    append_jsonl(failure_file, failure_rows)
    append_jsonl(link_file, link_rows)
    append_jsonl(navigation_file, navigation_rows)

    all_processed = processed | {row["audit_key"] for row in coverage_rows}
    pending_count = sum(1 for company in companies if company_audit_key(company) not in all_processed)
    if selected:
        next_index = (selected[-1][0] + 1) % len(companies)
    else:
        next_index = len(companies)

    state.update({
        "updated_at": utc_now(),
        "target_companies": 1000,
        "valid_seed_count": len(companies),
        "next_batch_start_index": next_index,
        "pending_seed_audits": pending_count,
        "last_auto_batch": {
            "requested_start_index": start,
            "selected_indices": [index for index, _ in selected],
            "selected_count": len(selected),
            "processed_count": len(coverage_rows),
            "job_detail_candidates": len(link_rows),
            "recruitment_navigation_links": len(navigation_rows),
            "max_workers": worker_count,
        },
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
            "new_job_link_default_status": "candidate_raw",
            "review_pending_requires_detail_validation": True,
            "official_entry_verification_requires_recruitment_signal": True,
            "cross_domain_candidate_requires_verified_seed_association": True,
        },
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "selected": len(selected),
        "processed": len(coverage_rows),
        "detail_links": len(link_rows),
        "navigation_links": len(navigation_rows),
        "pending_seed_audits": pending_count,
        "next_index": next_index,
        "max_workers": worker_count,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
