#!/usr/bin/env python3
"""Audit the next truly unprocessed company entries.

This wrapper scans from the checkpoint cursor, wraps once, and selects only
companies whose current company_id/career_url audit key is absent from the
coverage ledger. It preserves the conservative policy of crawl_company_jobs.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import requests

from crawl_company_jobs import (
    USER_AGENT,
    append_jsonl,
    existing_keys,
    fetch_page,
    load_companies,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=20)
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
    processed = existing_keys(coverage_file, "audit_key")
    selected = select_pending(companies, processed, start, args.batch_size)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})

    coverage_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    link_rows: list[dict[str, Any]] = []

    for _, company in selected:
        company_id = str(company.get("company_id") or "")
        name = str(company.get("company_name") or company.get("name") or "")
        career_url = str(company.get("career_url") or "")
        official_site = str(company.get("official_website") or career_url)
        audit_key = company_audit_key(company)

        if not career_url:
            observed_at = utc_now()
            coverage_rows.append({
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
                "error": "missing_career_url",
                "observed_at": observed_at,
                "final_acceptance_met": False,
            })
            failure_rows.append({
                "company_id": company_id,
                "company_name": name,
                "reason": "missing_career_url",
                "observed_at": observed_at,
            })
            continue

        result = fetch_page(session, career_url, official_site, career_url)
        verified = bool(
            result.http_status
            and 200 <= result.http_status < 400
            and result.final_url
            and same_official_family(result.final_url, official_site, career_url)
        )
        enumeration_status = "html_links_discovered" if result.job_links else "no_links_observed"
        if result.blocked:
            enumeration_status = "restricted"
        elif result.javascript_shell:
            enumeration_status = "pending_dynamic_js"
        elif result.error:
            enumeration_status = "fetch_failed"

        observed_at = utc_now()
        coverage_rows.append({
            "audit_key": audit_key,
            "company_id": company_id,
            "company_name": name,
            "official_entry_url": career_url,
            "final_url": result.final_url,
            "official_entry_verified": verified,
            "http_status": result.http_status,
            "page_title": result.title,
            "text_length": result.text_length,
            "enumeration_status": enumeration_status,
            "job_link_candidate_count": len(result.job_links),
            "error": result.error,
            "observed_at": observed_at,
            "final_acceptance_met": False,
        })
        if result.error or result.blocked or result.javascript_shell:
            failure_rows.append({
                "company_id": company_id,
                "company_name": name,
                "url": career_url,
                "reason": enumeration_status,
                "http_status": result.http_status,
                "observed_at": observed_at,
            })
        for job_url in result.job_links:
            link_rows.append({
                "company_id": company_id,
                "company_name": name,
                "source_url": job_url,
                "review_status": "review_pending",
                "discovered_at": observed_at,
            })

    append_jsonl(coverage_file, coverage_rows)
    append_jsonl(failure_file, failure_rows)
    append_jsonl(link_file, link_rows)

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
        },
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
        },
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "selected": len(selected),
        "processed": len(coverage_rows),
        "links": len(link_rows),
        "pending_seed_audits": pending_count,
        "next_index": next_index,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
