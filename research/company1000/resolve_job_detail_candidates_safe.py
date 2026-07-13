#!/usr/bin/env python3
"""Run the job-detail resolver with conservative audit-ledger compatibility.

The wrapper addresses two edge cases without weakening field validation:
1. preserve the originally discovered detail URL as ``source_url`` when an official
   portal redirects, recording the destination separately as ``final_url``;
2. retain numeric recruitment-article URLs that were classified as details using
   their link label, even though the label is no longer available to the resolver.

The underlying parser still rejects navigation pages, ambiguous multi-position
announcements and pages without duties or requirements. No access control is bypassed.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import resolve_job_detail_candidates as resolver

_original_schema_parser = resolver.parse_schema_posting
_original_html_parser = resolver.parse_html_posting
_original_url_classifier = resolver.is_probable_job_detail_url

LABELED_DETAIL_PATH = re.compile(
    r"/(?:blog|jobnews|jobs?news|recruit(?:ment)?|zhaopin|position|vacancy)/"
    r"(?:[^?#]*/)*(?:[a-z]*\d{4,})\.(?:s?html?|aspx?)$",
    re.I,
)


def _original_source(candidate: dict[str, Any], fallback: str) -> str:
    return str(candidate.get("source_url") or fallback).strip()


def classify_labeled_detail_without_label(url: str, label: str = "") -> bool:
    if _original_url_classifier(url, label):
        return True
    path = urlparse(url).path
    return bool(LABELED_DETAIL_PATH.search(path))


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


resolver.is_probable_job_detail_url = classify_labeled_detail_without_label
resolver.parse_schema_posting = parse_schema_with_canonical_source
resolver.parse_html_posting = parse_html_with_canonical_source


if __name__ == "__main__":
    raise SystemExit(resolver.main())
