#!/usr/bin/env python3
"""Curate discovered official job-detail candidates before detail resolution.

The navigation crawler intentionally has high recall. This pass removes only high-confidence
false positives, unwraps official share URLs, and deduplicates multilingual ATS mirrors.
Every exclusion is written to an auditable JSONL file. No job data is invented and no access
control is bypassed.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "source", "share", "shareid", "tracking", "ref", "referrer",
}
SHARE_KEYS = ("url", "shareUrl", "jobUrl", "redirect", "redirectUrl", "target")
JOB_ID_RE = re.compile(r"(?:/|=)(\d{4,12})(?:[/?#&]|$)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def normalized_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    query = parse_qs(parsed.query, keep_blank_values=True)
    filtered: list[tuple[str, str]] = []
    for key in sorted(query):
        if key.lower() in TRACKING_KEYS:
            continue
        for value in query[key]:
            filtered.append((key, value))
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse((scheme, host, path, "", urlencode(filtered, doseq=True), ""))


def unwrap_share_url(raw: str) -> tuple[str, str | None]:
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)
    if not (
        "sharejob" in parsed.path.lower()
        or "linkedinapi" in parsed.path.lower()
        or "share" in parsed.path.lower()
    ):
        return raw, None
    for key in SHARE_KEYS:
        values = query.get(key)
        if not values:
            continue
        candidate = unquote(values[0]).strip()
        nested = urlparse(candidate)
        if nested.scheme not in {"http", "https"} or not nested.netloc:
            continue
        nested_host = nested.netloc.lower()
        allowed = nested_host == host or nested_host.endswith("." + host) or host.endswith("." + nested_host)
        if allowed and re.search(r"jobdetail|job/|jobs/|position|vacanc", nested.path, re.I):
            return candidate, "official_share_wrapper_unwrapped"
    return raw, None


def exclusion_reason(company_id: str, url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host in {"www.fenbi.com", "fenbi.com"} and path.startswith("/page/positions/"):
        return "third_party_exam_position_not_employer_recruitment"
    if host in {"mokahr.com", "www.mokahr.com"} and path.startswith("/blog/"):
        return "ats_vendor_blog_not_employer_job_detail"
    if host in {"www.stec.net", "stec.net"} and path.startswith("/site/productcompositiondetail/"):
        return "product_or_project_content_not_recruitment"
    if "_linkedinapiv2" in path:
        return "social_share_api_not_job_detail"
    if path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2")):
        return "static_asset_not_job_detail"
    if re.search(r"/(?:privacy|terms|legal|cookie|sitemap|contact)(?:/|$)", path):
        return "generic_corporate_page_not_job_detail"
    return None


def locale_preference(url: str) -> int:
    path = urlparse(url).path.lower()
    if "/zh_cn/" in path:
        return 0
    if "/en_us/" in path or "/en_cn/" in path:
        return 1
    if re.search(r"/[a-z]{2}_[a-z]{2}/", path):
        return 3
    return 2


def dedupe_key(row: dict[str, Any]) -> tuple[str, str, str]:
    company_id = str(row.get("company_id") or "")
    url = str(row.get("source_url") or "")
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    if host in {"jobs.lenovo.com", "jobs.siemens.com"} and re.search(r"jobdetail", path, re.I):
        ids = JOB_ID_RE.findall(url)
        if ids:
            return company_id, host, "job_id:" + ids[-1]
    return company_id, host, url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    output = ROOT / args.output_dir
    candidate_paths = sorted(output.glob("*job_link_candidates*.jsonl"))
    all_rows: list[dict[str, Any]] = []
    for path in candidate_paths:
        all_rows.extend(read_jsonl(path))

    exclusions: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    unwrap_count = 0
    for row in all_rows:
        row = dict(row)
        raw = str(row.get("source_url") or "").strip()
        if not raw:
            exclusions.append({
                "company_id": row.get("company_id"),
                "company_name": row.get("company_name"),
                "source_url": None,
                "reason": "missing_source_url",
                "observed_at": utc_now(),
            })
            continue
        unwrapped, unwrap_reason = unwrap_share_url(raw)
        if unwrap_reason:
            unwrap_count += 1
            row["source_url_original"] = raw
            row["source_url"] = unwrapped
            row["normalization_note"] = unwrap_reason
        row["source_url"] = normalized_url(str(row.get("source_url") or ""))
        reason = exclusion_reason(str(row.get("company_id") or ""), str(row.get("source_url") or ""))
        if reason:
            exclusions.append({
                "company_id": row.get("company_id"),
                "company_name": row.get("company_name"),
                "source_url": row.get("source_url"),
                "source_url_original": row.get("source_url_original"),
                "reason": reason,
                "observed_at": utc_now(),
            })
            continue
        normalized_rows.append(row)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in normalized_rows:
        grouped.setdefault(dedupe_key(row), []).append(row)

    curated: list[dict[str, Any]] = []
    multilingual_duplicates = 0
    for key, rows in grouped.items():
        rows.sort(key=lambda item: (
            locale_preference(str(item.get("source_url") or "")),
            len(str(item.get("source_url") or "")),
            str(item.get("source_url") or ""),
        ))
        curated.append(rows[0])
        for duplicate in rows[1:]:
            multilingual_duplicates += 1
            exclusions.append({
                "company_id": duplicate.get("company_id"),
                "company_name": duplicate.get("company_name"),
                "source_url": duplicate.get("source_url"),
                "reason": "duplicate_multilingual_or_equivalent_job_url",
                "canonical_source_url": rows[0].get("source_url"),
                "observed_at": utc_now(),
            })

    curated.sort(key=lambda row: (
        str(row.get("company_id") or ""),
        str(row.get("source_url") or ""),
    ))

    curated_path = output / "job_link_candidates_curated.jsonl"
    for path in candidate_paths:
        if path != curated_path:
            write_jsonl(path, [])
    write_jsonl(curated_path, curated)

    exclusion_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in read_jsonl(output / "candidate_exclusions_auto.jsonl") + exclusions:
        key = (
            str(row.get("company_id") or ""),
            str(row.get("source_url") or ""),
            str(row.get("reason") or ""),
        )
        exclusion_index[key] = row
    exclusion_rows = sorted(exclusion_index.values(), key=lambda row: (
        str(row.get("company_id") or ""),
        str(row.get("source_url") or ""),
        str(row.get("reason") or ""),
    ))
    write_jsonl(output / "candidate_exclusions_auto.jsonl", exclusion_rows)

    reason_counts = Counter(str(row.get("reason") or "") for row in exclusions)
    state = {
        "generated_at": utc_now(),
        "input_candidate_rows": len(all_rows),
        "curated_candidate_rows": len(curated),
        "excluded_this_run": len(exclusions),
        "share_wrappers_unwrapped": unwrap_count,
        "multilingual_or_equivalent_duplicates_removed": multilingual_duplicates,
        "exclusion_reason_counts": dict(reason_counts),
        "policy": {
            "official_sources_only": True,
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "access_control_bypass_allowed": False,
            "only_high_confidence_false_positives_removed": True,
        },
    }
    runtime = ROOT / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "candidate_pruning_checkpoint.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
