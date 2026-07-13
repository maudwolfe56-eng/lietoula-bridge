#!/usr/bin/env python3
"""Retry-aware entry point for the bounded recruitment-navigation enumerator.

The v2 classifier performs the page analysis. This entry point changes ledger semantics:
only the latest resolution for a URL controls completion, and transport failures remain
retryable rather than being mistaken for a durable access restriction.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import enumerate_recruitment_navigation_v2 as nav

TRANSIENT_REASONS = {
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "ProxyError",
    "SSLError",
    "ChunkedEncodingError",
    "TooManyRedirects",
}
TERMINAL_STATUSES = {
    "enumerated",
    "no_current_openings_observed",
    "superseded",
    "not_job_navigation",
}


def resolution_key(row: dict[str, Any]) -> str:
    key = str(row.get("navigation_key") or "").strip()
    if key:
        return key
    return nav.nav_key(
        str(row.get("company_id") or ""),
        str(row.get("source_url") or row.get("url") or ""),
    )


def is_terminal_resolution(row: dict[str, Any]) -> bool:
    status = str(row.get("resolution_status") or "")
    if status in TERMINAL_STATUSES:
        return True
    if status != "restricted_with_explicit_reason":
        return False
    http_status = row.get("http_status")
    reason = str(row.get("reason") or "")
    return http_status in {401, 403, 429} or "access_control" in reason


def audited(
    row: dict[str, Any],
    company_by_id: dict[str, dict[str, Any]],
    max_children: int,
    timeout_seconds: int,
):
    resolution, details, children, failures = nav.audit_navigation(
        row, company_by_id, max_children, timeout_seconds
    )
    if (
        resolution.get("resolution_status") == "restricted_with_explicit_reason"
        and str(resolution.get("reason") or "") in TRANSIENT_REASONS
        and resolution.get("http_status") not in {401, 403, 429}
    ):
        resolution["resolution_status"] = "requires_retry_transient_fetch"
        for failure in failures:
            failure["reason"] = "requires_retry_transient_fetch"
            failure["detail"] = resolution.get("reason")
    return resolution, details, children, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--max-children-per-page", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=18)
    args = parser.parse_args()

    seed_rows = nav.load_companies(Path(args.seed_file))
    company_by_id = {str(row.get("company_id") or ""): row for row in seed_rows}
    output_dir = Path(args.output_dir)
    state_path = Path(args.state_file)

    explicit_navigation = nav.load_glob(output_dir, "*recruitment_navigation*.jsonl")
    legacy_navigation = nav.historical_navigation_candidates(output_dir)
    candidates = nav.canonical_candidates([*explicit_navigation, *legacy_navigation])
    resolution_file = output_dir / "recruitment_navigation_resolution_auto.jsonl"
    prior_resolutions = nav.read_jsonl(resolution_file)

    latest: dict[str, dict[str, Any]] = {}
    attempts: Counter[str] = Counter()
    for row in prior_resolutions:
        key = resolution_key(row)
        attempts[key] += 1
        latest[key] = row
    accepted = {key for key, row in latest.items() if is_terminal_resolution(row)}

    pending = [
        row for row in candidates
        if row["navigation_key"] not in accepted
        and attempts[row["navigation_key"]] < max(1, args.max_attempts)
    ]
    pending.sort(key=lambda row: (
        attempts[row["navigation_key"]],
        nav.normalized_depth(row),
        0 if nav.url_has_navigation_signal(str(row.get("source_url") or "")) else 1,
        str(row.get("company_id") or ""),
        str(row.get("source_url") or ""),
    ))
    pending = pending[: max(1, args.batch_size)]

    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(
                lambda row: audited(
                    row,
                    company_by_id,
                    max(1, args.max_children_per_page),
                    max(5, args.timeout_seconds),
                ),
                pending,
            ))

    resolutions: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for resolution, detail_rows, child_rows, failure_rows in results:
        resolutions.append(resolution)
        details.extend(detail_rows)
        children.extend(child_rows)
        failures.extend(failure_rows)

    nav.append_jsonl(resolution_file, resolutions)
    nav.append_jsonl(output_dir / "job_link_candidates_from_navigation.jsonl", details)
    nav.append_jsonl(output_dir / "recruitment_navigation_expanded.jsonl", children)
    nav.append_jsonl(output_dir / "failures_navigation.jsonl", failures)

    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    status_counts = Counter(str(row.get("resolution_status") or "unknown") for row in resolutions)
    state.update({
        "updated_at": nav.utc_now(),
        "navigation_expansion_last_batch": {
            "enumerator_version": "3.0-bounded-retry-aware",
            "candidate_count": len(candidates),
            "legacy_candidates_reclassified": len(legacy_navigation),
            "selected_count": len(pending),
            "processed_count": len(resolutions),
            "detail_candidates_discovered": len(details),
            "child_navigation_discovered": len(children),
            "filtered_non_recruitment_links": sum(
                int(row.get("filtered_non_recruitment_links") or 0) for row in resolutions
            ),
            "resolution_status_counts": dict(status_counts),
            "terminal_latest_resolution_count": len(accepted),
            "max_workers": worker_count,
            "max_children_per_page": max(1, args.max_children_per_page),
            "max_attempts": max(1, args.max_attempts),
        },
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state.get("navigation_expansion_last_batch", {}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
