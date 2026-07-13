#!/usr/bin/env python3
"""Enumerate official recruitment navigation pages conservatively.

The input ledger contains careers homepages, search/list pages and recruitment
channel pages. This resolver follows public links within the same official/ATS
family to a bounded depth, separates probable individual job details, and writes
an auditable navigation resolution. It does not log in, execute a CAPTCHA,
bypass access controls, or treat generic navigation as a job.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import requests

from crawl_company_jobs import (
    USER_AGENT,
    append_jsonl,
    fetch_page,
    is_probable_job_detail_url,
    same_official_family,
    utc_now,
)

ROOT = Path(__file__).resolve().parent
NO_OPENINGS = re.compile(
    r"暂无(?:在招|招聘|开放)?(?:职位|岗位)|当前暂无|没有(?:在招|开放)?(?:职位|岗位)|"
    r"no (?:current |open )?(?:jobs?|positions?|vacancies)|0\s*(?:个|条)?\s*(?:职位|岗位)",
    re.I,
)
RESOLVED_STATUSES = {
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
        rows.extend(read_jsonl(path))
    return rows


def unique_rows(rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = "|".join(str(row.get(field) or "") for field in fields)
        if key.strip("|"):
            index[key] = row
    return list(index.values())


def classify_legacy_navigation(row: dict[str, Any]) -> bool:
    source_type = str(row.get("source_type") or "")
    url = str(row.get("source_url") or "")
    if source_type == "official_recruitment_navigation":
        return True
    if source_type == "official_job_detail_candidate":
        return False
    return not is_probable_job_detail_url(url)


def enumerate_one(
    candidate: dict[str, Any],
    max_pages: int,
    max_depth: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_url = str(candidate.get("source_url") or "").strip()
    observed_at = utc_now()
    base = {
        "company_id": candidate.get("company_id"),
        "company_name": candidate.get("company_name"),
        "source_url": source_url,
        "observed_at": observed_at,
    }
    if not source_url:
        return {**base, "resolution_status": "not_job_navigation", "reason": "missing_url"}, []

    queue: deque[tuple[str, int]] = deque([(source_url, 0)])
    visited: set[str] = set()
    details: dict[str, dict[str, Any]] = {}
    navigation_seen: set[str] = set()
    errors: list[dict[str, Any]] = []
    no_openings_seen = False
    recruitment_signal_seen = False
    root_status: int | None = None
    root_blocked = False
    root_javascript_shell = False

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        result = fetch_page(session, url, source_url, source_url)
        if url == source_url:
            root_status = result.http_status
            root_blocked = result.blocked
            root_javascript_shell = result.javascript_shell

        if result.error:
            errors.append({"url": url, "reason": result.error, "http_status": result.http_status})
            continue
        if result.blocked:
            errors.append({"url": url, "reason": "restricted", "http_status": result.http_status})
            continue
        if result.http_status in {404, 410}:
            errors.append({"url": url, "reason": "superseded", "http_status": result.http_status})
            continue

        recruitment_signal_seen = recruitment_signal_seen or bool(result.recruitment_links)
        try:
            response = session.get(url, timeout=25, allow_redirects=True)
            if NO_OPENINGS.search(" ".join(response.text.split())[:20000]):
                no_openings_seen = True
        except requests.RequestException:
            pass

        for detail_url in result.job_links:
            details[detail_url] = {
                "company_id": candidate.get("company_id"),
                "company_name": candidate.get("company_name"),
                "source_url": detail_url,
                "source_type": "official_job_detail_candidate",
                "review_status": "candidate_raw",
                "promotion_eligible": False,
                "review_recommendation": "fetch_detail_and_validate_required_fields",
                "discovered_from": source_url,
                "discovered_at": observed_at,
            }

        if depth >= max_depth:
            continue
        for navigation_url in result.recruitment_links:
            if navigation_url in details or navigation_url in visited:
                continue
            if not same_official_family(navigation_url, source_url, source_url):
                continue
            navigation_seen.add(navigation_url)
            queue.append((navigation_url, depth + 1))

    if details:
        status = "enumerated"
        reason = "probable_job_detail_links_discovered"
    elif root_status in {404, 410}:
        status = "superseded"
        reason = f"root_http_{root_status}"
    elif root_blocked:
        status = "restricted_with_explicit_reason"
        reason = f"root_access_restricted_http_{root_status}"
    elif no_openings_seen:
        status = "no_current_openings_observed"
        reason = "explicit_no_openings_text_observed"
    elif not recruitment_signal_seen and not navigation_seen:
        status = "not_job_navigation"
        reason = "no_recruitment_or_job_signal_observed"
    elif root_javascript_shell:
        status = "pending_dynamic_js"
        reason = "javascript_shell_requires_public_api_or_browser_review"
    elif errors and len(visited) == len(errors):
        status = "pending_transient_fetch"
        reason = "all_bounded_fetches_failed"
    else:
        status = "pending_site_specific_enumerator"
        reason = "public_navigation_reached_but_no_detail_links_or_no_openings_signal"

    resolution = {
        **base,
        "resolution_status": status,
        "reason": reason,
        "root_http_status": root_status,
        "pages_visited": len(visited),
        "navigation_links_observed": len(navigation_seen),
        "job_detail_links_discovered": len(details),
        "errors": errors[:20],
        "policy": {
            "access_control_bypass_allowed": False,
            "auto_active_verified": False,
        },
    }
    return resolution, list(details.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-depth", type=int, default=2)
    args = parser.parse_args()

    output = ROOT / args.output_dir
    legacy = load_glob(output, "*job_link_candidates*.jsonl")
    explicit = load_glob(output, "*recruitment_navigation*.jsonl")
    candidates = unique_rows(
        [row for row in [*legacy, *explicit] if classify_legacy_navigation(row)],
        ("company_id", "source_url"),
    )
    previous = load_glob(output, "*recruitment_navigation_resolution*.jsonl")
    resolved_urls = {
        str(row.get("source_url") or "").strip()
        for row in previous
        if str(row.get("resolution_status") or "") in RESOLVED_STATUSES
    }
    pending = [
        row for row in candidates
        if str(row.get("source_url") or "").strip() not in resolved_urls
    ][: max(1, args.batch_size)]

    resolutions: list[dict[str, Any]] = []
    detail_candidates: list[dict[str, Any]] = []
    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(
                executor.map(
                    lambda row: enumerate_one(row, args.max_pages, args.max_depth),
                    pending,
                )
            )
        for resolution, details in results:
            resolutions.append(resolution)
            detail_candidates.extend(details)

    append_jsonl(output / "recruitment_navigation_resolution_auto.jsonl", resolutions)
    append_jsonl(output / "job_link_candidates_auto.jsonl", detail_candidates)

    completed = sum(1 for row in resolutions if row.get("resolution_status") in RESOLVED_STATUSES)
    state = {
        "updated_at": utc_now(),
        "navigation_candidates": len(candidates),
        "selected": len(pending),
        "completed_resolutions": completed,
        "detail_candidates_discovered": len(detail_candidates),
        "pending_after_batch_estimate": max(0, len(candidates) - len(resolved_urls) - completed),
        "max_pages_per_navigation": args.max_pages,
        "max_depth": args.max_depth,
        "policy": {
            "access_control_bypass_allowed": False,
            "auto_active_verified": False,
        },
    }
    runtime = ROOT / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "navigation_resolution_checkpoint.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
