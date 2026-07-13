#!/usr/bin/env python3
"""Retry-aware conservative job-detail resolver.

Only normalized jobs and terminal access/closure outcomes suppress future attempts.
Transient fetch failures, HTTP 5xx responses, and unparseable/multi-position pages remain
eligible for bounded retries. This aligns execution with the final acceptance gate and
prevents non-terminal failures from being silently treated as completed work.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import resolve_job_detail_candidates as resolver
import resolve_job_detail_candidates_safe as safe  # noqa: F401  Applies conservative patches.

ROOT = Path(__file__).resolve().parent
TERMINAL_REASONS = {
    "detail_access_restricted",
    "detail_closed_or_removed",
    "detail_not_found",
    "detail_gone",
}


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
    candidates = resolver.unique_rows(
        resolver.load_glob(output, "*job_link_candidates*.jsonl"),
        ("company_id", "source_url"),
    )
    candidates = [
        row for row in candidates
        if resolver.is_probable_job_detail_url(str(row.get("source_url") or ""))
    ]

    existing_jobs = resolver.load_glob(output, "*jobs*.jsonl")
    existing_failures = resolver.load_glob(output, "*failures*.jsonl")
    resolved_urls = {
        str(row.get("source_url") or "").strip()
        for row in existing_jobs
        if row.get("source_url")
    }
    terminal_urls = {
        str(row.get("source_url") or row.get("url") or "").strip()
        for row in existing_failures
        if (row.get("source_url") or row.get("url")) and terminal_failure(row)
    }
    attempts = Counter(
        str(row.get("source_url") or row.get("url") or "").strip()
        for row in existing_failures
        if row.get("source_url") or row.get("url")
    )

    pending = [
        row for row in candidates
        if str(row.get("source_url") or "").strip() not in resolved_urls
        and str(row.get("source_url") or "").strip() not in terminal_urls
        and attempts[str(row.get("source_url") or "").strip()] < max(1, args.max_attempts)
    ]
    pending.sort(key=lambda row: (
        attempts[str(row.get("source_url") or "").strip()],
        str(row.get("company_id") or ""),
        str(row.get("source_url") or ""),
    ))
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
    unresolved_after_batch = [
        row for row in candidates
        if str(row.get("source_url") or "").strip() not in resolved_urls
        and str(row.get("source_url") or "").strip() not in terminal_urls
    ]
    state = {
        "updated_at": resolver.utc_now(),
        "resolver_version": "2.0-retry-aware",
        "probable_detail_candidates": len(candidates),
        "resolved_job_urls_before_batch": len(resolved_urls),
        "terminal_failure_urls_before_batch": len(terminal_urls),
        "selected": len(pending),
        "job_records_created": len(job_rows),
        "terminal_failures_created": newly_terminal,
        "retryable_failures_created": retryable_failures,
        "remaining_unresolved_before_new_results": len(unresolved_after_batch),
        "max_attempts": max(1, args.max_attempts),
        "max_workers": worker_count,
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "access_control_bypass_allowed": False,
            "nonterminal_failures_remain_pending": True,
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
