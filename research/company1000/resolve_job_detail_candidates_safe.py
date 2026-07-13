#!/usr/bin/env python3
"""Run the job-detail resolver while preserving the discovered canonical URL.

Some official recruitment portals redirect a discovered detail URL. The base resolver
parses the final response URL, but the acceptance ledger is keyed by the originally
discovered URL. This wrapper keeps that original URL as ``source_url`` and records the
redirect destination separately as ``final_url`` so the candidate is resolved exactly
once without losing auditability.
"""
from __future__ import annotations

from typing import Any

import resolve_job_detail_candidates as resolver

_original_schema_parser = resolver.parse_schema_posting
_original_html_parser = resolver.parse_html_posting


def _original_source(candidate: dict[str, Any], fallback: str) -> str:
    return str(candidate.get("source_url") or fallback).strip()


def parse_schema_with_canonical_source(
    posting: dict[str, Any],
    candidate: dict[str, Any],
    response_url: str,
    observed_at: str,
) -> dict[str, Any] | None:
    job = _original_schema_parser(
        posting,
        candidate,
        _original_source(candidate, response_url),
        observed_at,
    )
    if job:
        job["final_url"] = response_url
    return job


def parse_html_with_canonical_source(
    soup: Any,
    candidate: dict[str, Any],
    response_url: str,
    observed_at: str,
) -> dict[str, Any] | None:
    job = _original_html_parser(
        soup,
        candidate,
        _original_source(candidate, response_url),
        observed_at,
    )
    if job:
        job["final_url"] = response_url
    return job


resolver.parse_schema_posting = parse_schema_with_canonical_source
resolver.parse_html_posting = parse_html_with_canonical_source


if __name__ == "__main__":
    raise SystemExit(resolver.main())
