#!/usr/bin/env python3
"""Classify official-link candidates before job-detail resolution.

The crawler's URL heuristics intentionally favor recall, so some official pages are not job
postings (for example product pages and ATS vendor blog posts). This script creates an
append-only suppression/routing ledger. It never deletes source evidence, never claims a
job exists, and never promotes a record beyond ``candidate_raw``.
"""
from __future__ import annotations

import argparse
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
    (re.compile(r"/(?:privacy|legal|terms|cookie|news|article|product|solution|service)(?:/|\?|$)", re.I), "non_recruitment_content_path"),
]
ANNOUNCEMENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/(?:JobNews|jobnews|recruit(?:ment)?[-_]?news|recruit(?:ment)?[-_]?notice|招聘公告|人才招聘公告)/", re.I), "official_multi_position_announcement"),
    (re.compile(r"/(?:notice|announcement)/[^?#]*(?:recruit|job|career)", re.I), "official_multi_position_announcement"),
]
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm", "from", "source", "ref", "referer", "tracking", "track"}
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


def load_candidates(output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("*job_link_candidates*.jsonl")):
        rows.extend(read_jsonl(path))
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        company_id = str(row.get("company_id") or "").strip()
        url = canonical_url(str(row.get("source_url") or ""))
        if company_id and url:
            normalized = dict(row)
            normalized["source_url"] = url
            unique[(company_id, url)] = normalized
    return list(unique.values())


def canonical_url(url: str) -> str:
    url = urldefrag(url)[0].strip()
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, urlencode(query, doseq=True), ""))


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def classify_static(url: str) -> tuple[str | None, str | None]:
    for pattern, reason in NON_JOB_PATTERNS:
        if pattern.search(url):
            return "suppress_non_job", reason
    for pattern, reason in ANNOUNCEMENT_PATTERNS:
        if pattern.search(url):
            return "route_announcement", reason
    return None, None


def lenovo_group_key(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "jobs.lenovo.com":
        return None
    match = LENOVO_DETAIL.match(parsed.path)
    if not match:
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

    existing: set[tuple[str, str, str]] = set()
    ledger_path = output_dir / "detail_candidate_classification.jsonl"
    for row in read_jsonl(ledger_path):
        existing.add((str(row.get("company_id") or ""), canonical_url(str(row.get("source_url") or "")), str(row.get("classification") or "")))

    rows: list[dict[str, Any]] = []
    observed_at = utc_now()
    grouped_lenovo: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)

    for candidate in candidates:
        company_id = str(candidate.get("company_id") or "")
        url = str(candidate.get("source_url") or "")
        classification, reason = classify_static(url)
        if classification:
            key = (company_id, url, classification)
            if key not in existing:
                rows.append({
                    "company_id": company_id,
                    "company_name": candidate.get("company_name"),
                    "source_url": url,
                    "classification": classification,
                    "reason": reason,
                    "terminal_for_detail_resolution": True,
                    "requires_announcement_resolution": classification == "route_announcement",
                    "review_status": "candidate_raw",
                    "observed_at": observed_at,
                })
                existing.add(key)
        group = lenovo_group_key(url)
        if group:
            grouped_lenovo[(company_id, *group)].append(candidate)

    for group_candidates in grouped_lenovo.values():
        if len(group_candidates) <= 1:
            continue
        preferred = min(group_candidates, key=lambda row: locale_rank(str(row.get("source_url") or "")))
        preferred_url = str(preferred.get("source_url") or "")
        for candidate in group_candidates:
            url = str(candidate.get("source_url") or "")
            if url == preferred_url:
                continue
            key = (str(candidate.get("company_id") or ""), url, "suppress_locale_duplicate")
            if key in existing:
                continue
            rows.append({
                "company_id": candidate.get("company_id"),
                "company_name": candidate.get("company_name"),
                "source_url": url,
                "classification": "suppress_locale_duplicate",
                "reason": "same_lenovo_job_id_already_represented_by_preferred_locale",
                "preferred_source_url": preferred_url,
                "terminal_for_detail_resolution": True,
                "requires_announcement_resolution": False,
                "review_status": "candidate_raw",
                "observed_at": observed_at,
            })
            existing.add(key)

    append_jsonl(ledger_path, rows)
    summary_counts: dict[str, int] = defaultdict(int)
    for row in read_jsonl(ledger_path):
        summary_counts[str(row.get("classification") or "unknown")] += 1
    report = {
        "generated_at": observed_at,
        "candidate_count": len(candidates),
        "new_classifications": len(rows),
        "classification_counts": dict(sorted(summary_counts.items())),
        "policy": {
            "source_evidence_deleted": False,
            "auto_active_verified": False,
            "announcement_routes_require_separate_resolution": True,
        },
    }
    runtime = Path("runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "detail_candidate_classification_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
