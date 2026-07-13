#!/usr/bin/env python3
"""Retry-aware detail resolver with explicit suppression/routing support.

Suppression, prior resolution, failure attempts and terminal outcomes are scoped by
(company_id, canonical source URL). This prevents a shared ATS or landing URL associated
with one company from incorrectly resolving or suppressing another company's evidence.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, urldefrag

import resolve_job_detail_candidates as resolver
import resolve_job_detail_candidates_safe as safe  # noqa: F401

ROOT = Path(__file__).resolve().parent
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "ref", "referer", "tracking", "track",
}
TERMINAL_REASONS = {
    "detail_access_restricted",
    "detail_closed_or_removed",
    "detail_not_found",
    "detail_gone",
}


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


def pair(row: dict[str, Any], *url_fields: str) -> tuple[str, str] | None:
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


def terminal_failure(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason") or "")
    status = row.get("http_status")
    if reason in TERMINAL_REASONS or reason.startswith("detail_access_restricted"):
        return True
    return reason == "detail_http_error" and status in {404, 410}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--batch-size", type=int, default=120)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()

    output = ROOT / args.output_dir
    raw_candidates = resolver.load_glob(output, "*job_link_candidates*.jsonl")
    candidate_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_candidates:
        candidate_pair = pair(row, "source_url")
        if not candidate_pair:
            continue
        company_id, url = candidate_pair
        if not resolver.is_probable_job_detail_url(url):
            continue
        normalized = dict(row)
        normalized["company_id"] = company_id
        normalized["source_url"] = url
        candidate_map[candidate_pair] = normalized
    candidates = list(candidate_map.values())

    classification_rows = read_jsonl(output / "detail_candidate_classification.jsonl")
    suppressed_pairs = {
        classified_pair
        for row in classification_rows
        if row.get("terminal_for_detail_resolution")
        for classified_pair in [pair(row, "source_url")]
        if classified_pair
    }
    routed_announcement_pairs = {
        classified_pair
        for row in classification_rows
        if row.get("requires_announcement_resolution")
        for classified_pair in [pair(row, "source_url")]
        if classified_pair
    }

    existing_jobs = resolver.load_glob(output, "*jobs*.jsonl")
    existing_failures = resolver.load_glob(output, "*failures*.jsonl")
    resolved_pairs = {
        job_pair
        for row in existing_jobs
        for job_pair in [pair(row, "source_url")]
        if job_pair
    }
    terminal_pairs = {
        failure_pair
        for row in existing_failures
        if terminal_failure(row)
        for failure_pair in [pair(row, "source_url", "url")]
        if failure_pair
    }
    attempts: Counter[tuple[str, str]] = Counter()
    for row in existing_failures:
        failure_pair = pair(row, "source_url", "url")
        if failure_pair:
            attempts[failure_pair] += 1

    pending = []
    for row in candidates:
        candidate_pair = pair(row, "source_url")
        if not candidate_pair:
            continue
        if candidate_pair in resolved_pairs or candidate_pair in terminal_pairs or candidate_pair in suppressed_pairs:
            continue
        if attempts[candidate_pair] >= max(1, args.max_attempts):
            continue
        pending.append(row)

    pending.sort(
        key=lambda row: (
            attempts[pair(row, "source_url") or ("", "")],
            str(row.get("company_id") or ""),
            canonical_url(str(row.get("source_url") or "")),
        )
    )
    pending = pending[: max(1, args.batch_size)]

    job_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(resolver.resolve_one, pending))
        for jobs, failures in results:
            job_rows.extend(jobs)
            failure_rows.extend(failures)

    resolver.append_jsonl(output / "jobs_resolved_auto.jsonl", job_rows)
    resolver.append_jsonl(output / "failures_job_details_auto.jsonl", failure_rows)

    newly_terminal = sum(1 for row in failure_rows if terminal_failure(row))
    retryable_failures = len(failure_rows) - newly_terminal
    state = {
        "updated_at": resolver.utc_now(),
        "resolver_version": "3.1-retry-aware-company-url-scoped-classification-ledger",
        "probable_detail_candidates": len(candidates),
        "suppressed_detail_pairs": len(suppressed_pairs),
        "routed_announcement_pairs": len(routed_announcement_pairs),
        "resolved_job_pairs_before_batch": len(resolved_pairs),
        "terminal_failure_pairs_before_batch": len(terminal_pairs),
        "selected": len(pending),
        "job_records_created": len(job_rows),
        "terminal_failures_created": newly_terminal,
        "retryable_failures_created": retryable_failures,
        "max_attempts": max(1, args.max_attempts),
        "max_workers": worker_count,
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "access_control_bypass_allowed": False,
            "nonterminal_failures_remain_pending": True,
            "classified_non_jobs_are_not_fetched_as_job_details": True,
            "official_announcements_are_resolved_separately": True,
            "resolution_scope": "company_id_plus_canonical_source_url",
        },
    }
    runtime = ROOT / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "detail_resolution_checkpoint.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
