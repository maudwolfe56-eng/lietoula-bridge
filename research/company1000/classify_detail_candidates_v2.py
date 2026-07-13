#!/usr/bin/env python3
"""Classify high-recall official-link candidates before resolution.

The output is append-only evidence. It never deletes source URLs, infers jobs, or promotes
records. Obvious non-job pages and locale duplicates are terminal only for the detail
resolver. Official recruitment notices are routed to the announcement resolver. URLs that
look like category/search landing pages are routed back to the navigation enumerator rather
than fetched as individual jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, urldefrag

from crawl_company_jobs import utc_now

NON_JOB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^https?://(?:www\.)?mokahr\.com/blog/", re.I), "ats_vendor_blog_not_job_posting"),
    (re.compile(r"^https?://(?:www\.)?stec\.net/site/productcompositiondetail/", re.I), "product_page_not_job_posting"),
    # Fenbi's /page/positions directory contains public-exam position reference data, not
    # corporate openings of Fenbi itself. It must not enter the enterprise job pool.
    (re.compile(r"^https?://(?:www\.)?fenbi\.com/page/positions/", re.I), "public_exam_position_directory_not_company_recruitment"),
    (re.compile(r"/(?:privacy|legal|terms|cookie)(?:/|\?|$)", re.I), "legal_or_privacy_page_not_job_posting"),
]
ANNOUNCEMENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/(?:JobNews|jobnews)/", re.I), "official_multi_position_announcement"),
    (re.compile(r"/(?:recruit(?:ment)?[-_]?news|recruit(?:ment)?[-_]?notice)/", re.I), "official_multi_position_announcement"),
    (re.compile(r"/(?:notice|announcement)/[^?#]*(?:recruit|job|career)", re.I), "official_multi_position_announcement"),
]
NAVIGATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/go/(?:All-Jobs|Search-Jobs)(?:/|$)", re.I), "ats_all_jobs_or_search_landing_page"),
    (re.compile(r"^https?://(?:www\.)?aptiv\.com/(?:[a-z]{2}/)?jobs/25for25/?$", re.I), "recruitment_campaign_landing_page"),
    (re.compile(r"^https?://career\.naura\.com/(?:\d+|custom/[^?#]+)?/?$", re.I), "official_career_category_or_custom_landing_page"),
    (re.compile(r"/(?:jobs?|careers?)/(?:all|search)?/?(?:\?.*)?$", re.I), "generic_job_search_landing_page"),
]
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "ref", "referer", "tracking", "track",
}
LENOVO_DETAIL = re.compile(r"^/([^/]+)/careers/JobDetail/([^/]+)/([0-9]+)$", re.I)
LOCALE_PREFERENCE = {"zh_CN": 0, "en_US": 1, "en_GB": 2}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def canonical_url(url: str) -> str:
    url = urldefrag(url)[0].strip()
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS
    ]
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            "",
        )
    )


def nav_key(company_id: str, url: str) -> str:
    return hashlib.sha256(f"{company_id}|{canonical_url(url)}".encode()).hexdigest()[:20]


def load_candidates(output_dir: Path) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(output_dir.glob("*job_link_candidates*.jsonl")):
        for row in read_jsonl(path):
            company_id = str(row.get("company_id") or "").strip()
            url = canonical_url(str(row.get("source_url") or ""))
            if company_id and url.startswith(("http://", "https://")):
                normalized = dict(row)
                normalized["source_url"] = url
                unique[(company_id, url)] = normalized
    return list(unique.values())


def static_classification(url: str) -> tuple[str | None, str | None]:
    for pattern, reason in NON_JOB_PATTERNS:
        if pattern.search(url):
            return "suppress_non_job", reason
    for pattern, reason in ANNOUNCEMENT_PATTERNS:
        if pattern.search(url):
            return "route_announcement", reason
    for pattern, reason in NAVIGATION_PATTERNS:
        if pattern.search(url):
            return "route_navigation", reason
    return None, None


def lenovo_group_key(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    match = LENOVO_DETAIL.match(parsed.path)
    if parsed.netloc.lower() != "jobs.lenovo.com" or not match:
        return None
    return parsed.netloc.lower(), match.group(3)


def locale_rank(url: str) -> tuple[int, str]:
    match = LENOVO_DETAIL.match(urlparse(url).path)
    locale = match.group(1) if match else ""
    return LOCALE_PREFERENCE.get(locale, 100), locale


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    candidates = load_candidates(output_dir)
    ledger_path = output_dir / "detail_candidate_classification.jsonl"

    existing: set[tuple[str, str, str]] = set()
    for row in read_jsonl(ledger_path):
        existing.add(
            (
                str(row.get("company_id") or ""),
                canonical_url(str(row.get("source_url") or "")),
                str(row.get("classification") or ""),
            )
        )

    observed_at = utc_now()
    classifications: list[dict[str, Any]] = []
    nav_resolutions: list[dict[str, Any]] = []
    navigation_routes: list[dict[str, Any]] = []
    announcement_failures: list[dict[str, Any]] = []
    grouped_lenovo: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)

    def add_classification(
        candidate: dict[str, Any],
        classification: str,
        reason: str,
        preferred_url: str | None = None,
    ) -> None:
        company_id = str(candidate.get("company_id") or "")
        company_name = candidate.get("company_name")
        url = canonical_url(str(candidate.get("source_url") or ""))
        item_key = (company_id, url, classification)
        if item_key in existing:
            return
        requires_announcement = classification == "route_announcement"
        requires_navigation = classification == "route_navigation"
        row = {
            "company_id": company_id,
            "company_name": company_name,
            "source_url": url,
            "classification": classification,
            "reason": reason,
            "preferred_source_url": preferred_url,
            "terminal_for_detail_resolution": True,
            "requires_announcement_resolution": requires_announcement,
            "requires_navigation_resolution": requires_navigation,
            "review_status": "candidate_raw",
            "observed_at": observed_at,
        }
        classifications.append(row)
        existing.add(item_key)

        if requires_navigation:
            navigation_routes.append({
                "audit_key": nav_key(company_id, url),
                "company_id": company_id,
                "company_name": company_name,
                "source_url": url,
                "source_type": "official_recruitment_navigation",
                "navigation_depth": candidate.get("navigation_depth", 0),
                "parent_url": candidate.get("parent_url"),
                "discovery_reason": reason,
                "review_status": "candidate_raw",
                "observed_at": observed_at,
            })
            return

        resolution_status = (
            "enumerated"
            if requires_announcement
            else ("superseded" if classification == "suppress_locale_duplicate" else "not_job_navigation")
        )
        nav_resolutions.append({
            "navigation_key": nav_key(company_id, url),
            "company_id": company_id,
            "company_name": company_name,
            "source_url": url,
            "final_url": None,
            "http_status": None,
            "page_title": None,
            "text_length": 0,
            "navigation_depth": candidate.get("navigation_depth"),
            "resolution_status": resolution_status,
            "reason": reason,
            "self_detail_candidate": requires_announcement,
            "job_detail_candidates_discovered": 0,
            "child_navigation_discovered": 0,
            "filtered_non_recruitment_links": 0,
            "observed_at": observed_at,
        })
        if requires_announcement:
            announcement_failures.append({
                "company_id": company_id,
                "company_name": company_name,
                "url": url,
                "source_url": url,
                "reason": "detail_unparseable_or_multi_position_announcement",
                "detail": "routed_by_official_announcement_url_classifier",
                "http_status": None,
                "observed_at": observed_at,
            })

    for candidate in candidates:
        classification, reason = static_classification(str(candidate.get("source_url") or ""))
        if classification and reason:
            add_classification(candidate, classification, reason)
        group = lenovo_group_key(str(candidate.get("source_url") or ""))
        if group:
            grouped_lenovo[(str(candidate.get("company_id") or ""), *group)].append(candidate)

    for group_candidates in grouped_lenovo.values():
        if len(group_candidates) <= 1:
            continue
        preferred = min(group_candidates, key=lambda row: locale_rank(str(row.get("source_url") or "")))
        preferred_url = canonical_url(str(preferred.get("source_url") or ""))
        for candidate in group_candidates:
            url = canonical_url(str(candidate.get("source_url") or ""))
            if url != preferred_url:
                add_classification(
                    candidate,
                    "suppress_locale_duplicate",
                    "same_lenovo_job_id_already_represented_by_preferred_locale",
                    preferred_url,
                )

    append_jsonl(ledger_path, classifications)
    append_jsonl(output_dir / "recruitment_navigation_resolution_auto.jsonl", nav_resolutions)
    append_jsonl(output_dir / "recruitment_navigation_classified.jsonl", navigation_routes)
    append_jsonl(output_dir / "failures_routed_announcements.jsonl", announcement_failures)

    counts: dict[str, int] = defaultdict(int)
    for row in read_jsonl(ledger_path):
        counts[str(row.get("classification") or "unknown")] += 1
    report = {
        "generated_at": observed_at,
        "candidate_count": len(candidates),
        "new_classifications": len(classifications),
        "new_navigation_resolutions": len(nav_resolutions),
        "new_navigation_routes": len(navigation_routes),
        "new_announcement_routes": len(announcement_failures),
        "classification_counts": dict(sorted(counts.items())),
        "policy": {
            "source_evidence_deleted": False,
            "auto_active_verified": False,
            "announcement_routes_require_separate_resolution": True,
            "navigation_routes_require_site_specific_resolution": True,
        },
    }
    runtime = Path("runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "detail_candidate_classification_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
