#!/usr/bin/env python3
"""Classification-aware final acceptance assessment.

The base assessor is executed first for schema and core integrity checks. This wrapper then
replaces the two high-recall queue calculations with latest-ledger calculations, and adds an
explicit gate for routed multi-position announcements.

Conservative rules:
- explicit non-job pages and locale duplicates do not remain in the detail queue;
- routed official announcements leave the detail queue but remain pending until role-level
  records are extracted or a terminal access/closure reason is recorded;
- only the latest navigation resolution controls navigation completion;
- transient fetch, parsing and attachment-extraction failures remain pending;
- no record is automatically marked active_verified and no undisclosed salary is inferred.
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
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "ref", "referer", "tracking", "track",
}
NAV_TERMINAL = {
    "enumerated",
    "no_current_openings_observed",
    "superseded",
    "not_job_navigation",
}
DETAIL_TERMINAL_REASONS = {
    "detail_access_restricted",
    "detail_closed_or_removed",
    "detail_not_found",
    "detail_gone",
}
ANNOUNCEMENT_TERMINAL_REASONS = {
    "announcement_access_restricted",
    "announcement_closed_or_removed",
    "announcement_not_found",
    "announcement_gone",
}


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


def company_url_pair(row: dict[str, Any], *url_fields: str) -> tuple[str, str] | None:
    company_id = str(row.get("company_id") or "").strip()
    raw_url = ""
    for field in url_fields:
        if row.get(field):
            raw_url = str(row.get(field) or "")
            break
    url = canonical_url(raw_url)
    if not company_id or not url.startswith(("http://", "https://")):
        return None
    return company_id, url


def nav_key(company_id: str, url: str) -> str:
    return hashlib.sha256(f"{company_id}|{canonical_url(url)}".encode()).hexdigest()[:20]


def detail_failure_terminal(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason") or "")
    status = row.get("http_status")
    if reason in DETAIL_TERMINAL_REASONS or reason.startswith("detail_access_restricted"):
        return True
    return reason == "detail_http_error" and status in {404, 410}


def announcement_failure_terminal(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason") or "")
    status = row.get("http_status")
    if reason in ANNOUNCEMENT_TERMINAL_REASONS or reason.startswith("announcement_access_restricted"):
        return True
    return reason == "announcement_http_error" and status in {404, 410}


def navigation_resolution_terminal(row: dict[str, Any]) -> bool:
    status = str(row.get("resolution_status") or "")
    if status in NAV_TERMINAL:
        return True
    if status != "restricted_with_explicit_reason":
        return False
    reason = str(row.get("reason") or "")
    http_status = row.get("http_status")
    return http_status in {401, 403, 429} or "access_control" in reason or "restricted" in reason


def sample_pairs(rows: set[tuple[str, str]], limit: int = 50) -> list[dict[str, str]]:
    return [
        {"company_id": company_id, "source_url": url}
        for company_id, url in sorted(rows)[:limit]
    ]


def main() -> int:
    subprocess.run([sys.executable, str(ROOT / "assess_final_acceptance.py")], cwd=ROOT, check=True)
    report_path = DELIVERABLES / "final_acceptance_report.json"
    report = read_json(report_path, {})
    if not isinstance(report, dict):
        report = {}

    classifications = read_jsonl(OUTPUT / "detail_candidate_classification.jsonl")
    suppressed_detail_pairs: set[tuple[str, str]] = set()
    routed_announcement_pairs: set[tuple[str, str]] = set()
    for row in classifications:
        pair = company_url_pair(row, "source_url")
        if not pair:
            continue
        if row.get("terminal_for_detail_resolution"):
            suppressed_detail_pairs.add(pair)
        if row.get("requires_announcement_resolution"):
            routed_announcement_pairs.add(pair)

    candidate_rows = load_glob("*job_link_candidates*.jsonl")
    detail_candidates: set[tuple[str, str]] = set()
    navigation_candidates: set[tuple[str, str]] = set()
    for row in candidate_rows:
        pair = company_url_pair(row, "source_url")
        if not pair:
            continue
        _, url = pair
        source_type = str(row.get("source_type") or "")
        if source_type == "official_recruitment_navigation":
            navigation_candidates.add(pair)
        elif detail_resolver.is_probable_job_detail_url(url):
            detail_candidates.add(pair)
        else:
            navigation_candidates.add(pair)

    for row in load_glob("*recruitment_navigation*.jsonl", exclude=("resolution",)):
        pair = company_url_pair(row, "source_url", "url")
        if pair:
            navigation_candidates.add(pair)

    job_rows = load_glob("*jobs*.jsonl")
    resolved_detail_pairs: set[tuple[str, str]] = set()
    resolved_announcement_pairs: set[tuple[str, str]] = set()
    for row in job_rows:
        detail_pair = company_url_pair(row, "source_url")
        if detail_pair:
            resolved_detail_pairs.add(detail_pair)
        announcement_pair = company_url_pair(row, "source_page_url")
        if announcement_pair:
            resolved_announcement_pairs.add(announcement_pair)
        elif str(row.get("source_type") or "") == "official_recruitment_announcement":
            source_pair = company_url_pair(row, "source_url")
            if source_pair:
                resolved_announcement_pairs.add(source_pair)

    failure_rows = load_glob("*failures*.jsonl")
    terminal_detail_pairs: set[tuple[str, str]] = set()
    terminal_announcement_pairs: set[tuple[str, str]] = set()
    for row in failure_rows:
        pair = company_url_pair(row, "source_url", "url")
        if not pair:
            continue
        if detail_failure_terminal(row):
            terminal_detail_pairs.add(pair)
        if announcement_failure_terminal(row):
            terminal_announcement_pairs.add(pair)

    pending_detail = {
        pair
        for pair in detail_candidates
        if pair not in resolved_detail_pairs
        and pair not in terminal_detail_pairs
        and pair not in suppressed_detail_pairs
    }
    pending_announcements = {
        pair
        for pair in routed_announcement_pairs
        if pair not in resolved_announcement_pairs
        and pair not in terminal_announcement_pairs
    }

    latest_resolution: dict[str, dict[str, Any]] = {}
    for row in load_glob("*recruitment_navigation_resolution*.jsonl"):
        pair = company_url_pair(row, "source_url", "url")
        if not pair:
            continue
        company_id, url = pair
        latest_resolution[nav_key(company_id, url)] = row
    terminal_nav_keys = {
        key for key, row in latest_resolution.items() if navigation_resolution_terminal(row)
    }
    pending_navigation = {
        pair
        for pair in navigation_candidates
        if nav_key(pair[0], pair[1]) not in terminal_nav_keys
    }

    conditions = dict(report.get("conditions") or {})
    detail_complete = len(pending_detail) == 0
    navigation_complete = len(pending_navigation) == 0
    announcement_complete = len(pending_announcements) == 0

    # Replace the legacy high-recall gates instead of merely adding parallel keys.
    conditions["all_discovered_job_detail_links_resolved"] = detail_complete
    conditions["all_recruitment_navigation_enumerated"] = navigation_complete
    conditions["all_probable_job_details_resolved"] = detail_complete
    conditions["all_recruitment_navigation_resolved"] = navigation_complete
    conditions["all_routed_announcements_resolved"] = announcement_complete

    final_acceptance = all(bool(value) for value in conditions.values())
    report["conditions"] = conditions
    report["final_acceptance_met"] = final_acceptance
    report["final_acceptance"] = final_acceptance
    report["notification_eligible"] = final_acceptance
    report["assessment_version"] = "2.1-classification-aware-latest-ledger-announcement-gate"
    counts = report.setdefault("counts", {})
    counts["probable_job_detail_candidates"] = len(detail_candidates)
    counts["classified_detail_urls_excluded"] = len(suppressed_detail_pairs)
    counts["routed_announcement_urls"] = len(routed_announcement_pairs)
    counts["resolved_routed_announcements"] = len(routed_announcement_pairs - pending_announcements)
    counts["pending_routed_announcements"] = len(pending_announcements)
    counts["pending_job_detail_candidates"] = len(pending_detail)
    counts["recruitment_navigation_candidates"] = len(navigation_candidates)
    counts["terminal_latest_navigation_resolutions"] = len(terminal_nav_keys)
    counts["pending_recruitment_navigation"] = len(pending_navigation)
    report["pending_samples"] = {
        "job_details": sample_pairs(pending_detail),
        "routed_announcements": sample_pairs(pending_announcements),
        "recruitment_navigation": sample_pairs(pending_navigation),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint_path = RUNTIME / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, {})
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    checkpoint.update({
        "pending_job_link_candidates": len(pending_detail),
        "pending_recruitment_navigation": len(pending_navigation),
        "pending_routed_announcements": len(pending_announcements),
        "classified_detail_urls_excluded": len(suppressed_detail_pairs),
        "routed_announcement_urls": len(routed_announcement_pairs),
        "final_acceptance_met": final_acceptance,
        "final_acceptance": final_acceptance,
        "notification_eligible": final_acceptance,
    })
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = DELIVERABLES / "run_summary.json"
    summary = read_json(summary_path, {})
    if not isinstance(summary, dict):
        summary = {}
    summary.update({
        "final_acceptance_met": final_acceptance,
        "final_acceptance": final_acceptance,
        "notification_eligible": final_acceptance,
        "final_acceptance_report": report,
    })
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "final_acceptance": final_acceptance,
        "pending_job_detail_candidates": len(pending_detail),
        "pending_routed_announcements": len(pending_announcements),
        "pending_recruitment_navigation": len(pending_navigation),
        "classified_detail_urls_excluded": len(suppressed_detail_pairs),
        "routed_announcement_urls": len(routed_announcement_pairs),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
