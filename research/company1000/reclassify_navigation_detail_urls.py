#!/usr/bin/env python3
"""Move strongly identifiable ATS detail URLs out of the navigation queue.

This performs URL-shape classification only. It does not claim the page is a valid current
opening and does not promote beyond ``candidate_raw``. The normal detail resolver must
still fetch the official page and validate title, duties, requirements, location and time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urldefrag, urlparse

from crawl_company_jobs import append_jsonl, utc_now

STRONG_DETAIL_PATH = re.compile(
    r"getOnePosition|(?:job|position)(?:Detail|Info|View)|view(?:Job|Position)|"
    r"/jobs?/(?:detail/)?[^/?#]+|/positions?/(?:detail/)?[^/?#]+|"
    r"/requisitions?/[^/?#]+|/vacanc(?:y|ies)/[^/?#]+",
    re.I,
)
LIST_OR_NONJOB_PATH = re.compile(
    r"getPositionList|search|privacy|login|index(?:\?|$)|news|policy|"
    r"alternativePosition|social(?:/|$)|campus(?:/|$)|graduate(?:/|$)",
    re.I,
)
DETAIL_QUERY_KEYS = {
    "postidenc", "postid", "jobid", "job_id", "positionid", "position_id",
    "requisitionid", "requisition_id", "reqid", "vacancyid", "vacancy_id",
    "postingid", "posting_id",
}


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


def load_navigation_rows(output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("*recruitment_navigation*.jsonl")):
        if "resolution" in path.name:
            continue
        rows.extend(read_jsonl(path))
    return rows


def canonical(url: str) -> str:
    return urldefrag(url)[0].strip()


def key(company_id: str, url: str) -> str:
    return hashlib.sha256(f"{company_id}|{canonical(url)}".encode()).hexdigest()[:20]


def is_strong_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    target = f"{parsed.path}?{parsed.query}"
    if LIST_OR_NONJOB_PATH.search(target) and not re.search(r"getOnePosition", target, re.I):
        return False
    query = {name.lower(): values for name, values in parse_qs(parsed.query).items()}
    has_detail_query = any(
        name in DETAIL_QUERY_KEYS and any(str(value).strip() for value in values)
        for name, values in query.items()
    )
    return bool(STRONG_DETAIL_PATH.search(target) or has_detail_query)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    nav_rows = load_navigation_rows(output_dir)
    existing_candidates: set[tuple[str, str]] = set()
    for path in sorted(output_dir.glob("*job_link_candidates*.jsonl")):
        for row in read_jsonl(path):
            company_id = str(row.get("company_id") or "").strip()
            url = canonical(str(row.get("source_url") or ""))
            if company_id and url:
                existing_candidates.add((company_id, url))

    candidate_rows: list[dict[str, Any]] = []
    resolution_rows: list[dict[str, Any]] = []
    observed_at = utc_now()
    seen: set[tuple[str, str]] = set()
    for row in nav_rows:
        company_id = str(row.get("company_id") or "").strip()
        company_name = str(row.get("company_name") or "").strip()
        url = canonical(str(row.get("source_url") or row.get("url") or ""))
        item_key = (company_id, url)
        if not company_id or not url or item_key in seen or not is_strong_detail_url(url):
            continue
        seen.add(item_key)
        if item_key not in existing_candidates:
            candidate_rows.append({
                "company_id": company_id,
                "company_name": company_name,
                "source_url": url,
                "source_type": "official_job_detail_candidate",
                "review_status": "candidate_raw",
                "promotion_eligible": False,
                "review_recommendation": "fetch_detail_and_validate_required_fields",
                "discovered_via": str(row.get("parent_navigation_url") or row.get("source_url") or ""),
                "discovered_at": observed_at,
                "classification_reason": "strong_official_ats_detail_url_shape",
            })
        resolution_rows.append({
            "navigation_key": key(company_id, url),
            "company_id": company_id,
            "company_name": company_name,
            "source_url": url,
            "final_url": None,
            "http_status": None,
            "page_title": None,
            "text_length": 0,
            "navigation_depth": row.get("navigation_depth"),
            "resolution_status": "enumerated",
            "reason": "strong_detail_url_reclassified_without_content_claim",
            "self_detail_candidate": True,
            "job_detail_candidates_discovered": 1,
            "child_navigation_discovered": 0,
            "filtered_non_recruitment_links": 0,
            "observed_at": observed_at,
        })

    append_jsonl(output_dir / "job_link_candidates_reclassified_from_navigation.jsonl", candidate_rows)
    append_jsonl(output_dir / "recruitment_navigation_resolution_auto.jsonl", resolution_rows)
    summary = {
        "observed_at": observed_at,
        "navigation_rows_scanned": len(nav_rows),
        "strong_detail_urls_identified": len(seen),
        "new_detail_candidates_created": len(candidate_rows),
        "navigation_resolutions_created": len(resolution_rows),
        "review_status": "candidate_raw",
        "content_validation_completed": False,
    }
    runtime = Path("runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "navigation_detail_reclassification.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
