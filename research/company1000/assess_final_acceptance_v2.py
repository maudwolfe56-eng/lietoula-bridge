#!/usr/bin/env python3
"""Classification-aware final acceptance assessment.

Runs the existing assessment first, then replaces the two high-recall queue calculations
with conservative latest-ledger calculations:
- explicit non-job, locale-duplicate and announcement-routed URLs do not remain in the
  job-detail queue;
- only the latest navigation resolution controls whether a navigation URL is complete;
- transient failures remain pending.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, urldefrag

import resolve_job_detail_candidates as detail_resolver

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
RUNTIME = ROOT / "runtime"
DELIVERABLES = ROOT / "deliverables"
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm", "from", "source", "ref", "referer", "tracking", "track"}
NAV_TERMINAL = {"enumerated", "no_current_openings_observed", "superseded", "not_job_navigation"}
DETAIL_TERMINAL_REASONS = {"detail_access_restricted", "detail_closed_or_removed", "detail_not_found", "detail_gone"}


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


def load_glob(pattern: str, exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(OUTPUT.glob(pattern)):
        if any(token in path.name for token in exclude):
            continue
        rows.extend(read_jsonl(path))
    return rows


def canonical_url(url: str) -> str:
    url = urldefrag(url)[0].strip()
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, urlencode(query, doseq=True), ""))


def nav_key(company_id: str, url: str) -> str:
    return hashlib.sha256(f"{company_id}|{canonical_url(url)}".encode()).hexdigest()[:20]


def detail_failure_terminal(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason") or "")
    status = row.get("http_status")
    if reason in DETAIL_TERMINAL_REASONS or reason.startswith("detail_access_restricted"):
        return True
    return reason == "detail_http_error" and status in {404, 410}


def navigation_resolution_terminal(row: dict[str, Any]) -> bool:
    status = str(row.get("resolution_status") or "")
    if status in NAV_TERMINAL:
        return True
    if status != "restricted_with_explicit_reason":
        return False
    reason = str(row.get("reason") or "")
    http_status = row.get("http_status")
    return http_status in {401, 403, 429} or "access_control" in reason


def main() -> int:
    subprocess.run([sys.executable, str(ROOT / "assess_final_acceptance.py")], cwd=ROOT, check=True)
    report_path = DELIVERABLES / "final_acceptance_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    classifications = read_jsonl(OUTPUT / "detail_candidate_classification.jsonl")
    suppressed_detail_urls = {
        canonical_url(str(row.get("source_url") or ""))
        for row in classifications
        if row.get("terminal_for_detail_resolution") and row.get("source_url")
    }
    routed_announcement_urls = {
        canonical_url(str(row.get("source_url") or ""))
        for row in classifications
        if row.get("requires_announcement_resolution") and row.get("source_url")
    }

    candidate_rows = load_glob("*job_link_candidates*.jsonl")
    detail_candidates: set[tuple[str, str]] = set()
    for row in candidate_rows:
        company_id = str(row.get("company_id") or "").strip()
        url = canonical_url(str(row.get("source_url") or ""))
        if company_id and url and detail_resolver.is_probable_job_detail_url(url):
            detail_candidates.add((company_id, url))

    resolved_urls = {
        canonical_url(str(row.get("source_url") or ""))
        for row in load_glob("*jobs*.jsonl")
        if row.get("source_url")
    }
    terminal_failure_urls = {
        canonical_url(str(row.get("source_url") or row.get("url") or ""))
        for row in load_glob("*failures*.jsonl")
        if (row.get("source_url") or row.get("url")) and detail_failure_terminal(row)
    }
    pending_detail = {
        (company_id, url)
        for company_id, url in detail_candidates
        if url not in resolved_urls
        and url not in terminal_failure_urls
        and url not in suppressed_detail_urls
    }

    nav_candidates: set[tuple[str, str]] = set()
    for row in load_glob("*recruitment_navigation*.jsonl", exclude=("resolution",)):
        company_id = str(row.get("company_id") or "").strip()
        url = canonical_url(str(row.get("source_url") or row.get("url") or ""))
        if company_id and url:
            nav_candidates.add((company_id, url))
    latest_resolution: dict[str, dict[str, Any]] = {}
    for row in load_glob("*recruitment_navigation_resolution*.jsonl"):
        company_id = str(row.get("company_id") or "").strip()
        url = canonical_url(str(row.get("source_url") or row.get("url") or ""))
        key = str(row.get("navigation_key") or nav_key(company_id, url))
        if company_id and url:
            latest_resolution[key] = row
    terminal_nav_keys = {key for key, row in latest_resolution.items() if navigation_resolution_terminal(row)}
    pending_nav = {
        (company_id, url)
        for company_id, url in nav_candidates
        if nav_key(company_id, url) not in terminal_nav_keys
    }

    conditions = dict(report.get("conditions") or {})
    conditions["all_recruitment_navigation_resolved"] = len(pending_nav) == 0
    conditions["all_probable_job_details_resolved"] = len(pending_detail) == 0
    final_acceptance = all(bool(value) for value in conditions.values())

    report["conditions"] = conditions
    report["final_acceptance"] = final_acceptance
    report["notification_eligible"] = final_acceptance
    report["assessment_version"] = "2.0-classification-aware-latest-ledger"
    report.setdefault("counts", {})["probable_job_detail_candidates"] = len(detail_candidates)
    report["counts"]["classified_detail_urls_excluded"] = len(suppressed_detail_urls)
    report["counts"]["routed_announcement_urls"] = len(routed_announcement_urls)
    report["counts"]["pending_job_detail_candidates"] = len(pending_detail)
    report["counts"]["recruitment_navigation_candidates"] = len(nav_candidates)
    report["counts"]["terminal_latest_navigation_resolutions"] = len(terminal_nav_keys)
    report["counts"]["pending_recruitment_navigation"] = len(pending_nav)
    report["pending_samples"] = {
        "job_details": [
            {"company_id": company_id, "source_url": url}
            for company_id, url in sorted(pending_detail)[:50]
        ],
        "recruitment_navigation": [
            {"company_id": company_id, "source_url": url}
            for company_id, url in sorted(pending_nav)[:50]
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint_path = RUNTIME / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {}
    checkpoint["pending_job_link_candidates"] = len(pending_detail)
    checkpoint["pending_recruitment_navigation"] = len(pending_nav)
    checkpoint["classified_detail_urls_excluded"] = len(suppressed_detail_urls)
    checkpoint["routed_announcement_urls"] = len(routed_announcement_urls)
    checkpoint["final_acceptance"] = final_acceptance
    checkpoint["notification_eligible"] = final_acceptance
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "final_acceptance": final_acceptance,
        "pending_job_detail_candidates": len(pending_detail),
        "pending_recruitment_navigation": len(pending_nav),
        "classified_detail_urls_excluded": len(suppressed_detail_urls),
        "routed_announcement_urls": len(routed_announcement_urls),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
