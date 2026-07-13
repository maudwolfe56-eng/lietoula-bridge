#!/usr/bin/env python3
"""Create a small diagnostic summary from the append-only detail failure ledger."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from crawl_company_jobs import utc_now

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
RUNTIME = ROOT / "runtime"


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


def path_shape(url: str) -> str:
    parsed = urlparse(url)
    parts = []
    for part in parsed.path.split("/"):
        if not part:
            continue
        if len(part) > 24 or any(ch.isdigit() for ch in part):
            parts.append("{id}")
        else:
            parts.append(part.lower())
    query_keys = sorted(key.lower() for key in parse_qs(parsed.query))
    suffix = "?" + "&".join(query_keys) if query_keys else ""
    return "/" + "/".join(parts[:8]) + suffix


def main() -> int:
    rows = read_jsonl(OUTPUT / "failures_job_details_auto.jsonl")
    reasons = Counter(str(row.get("reason") or "unknown") for row in rows)
    hosts = Counter(urlparse(str(row.get("source_url") or row.get("url") or "")).netloc.lower() for row in rows)
    shapes = Counter(path_shape(str(row.get("source_url") or row.get("url") or "")) for row in rows)
    statuses = Counter(str(row.get("http_status") or "none") for row in rows)

    latest_by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        url = str(row.get("source_url") or row.get("url") or "").strip()
        if url:
            latest_by_url[url] = row
    latest_reasons = Counter(str(row.get("reason") or "unknown") for row in latest_by_url.values())

    samples_by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    samples_by_host: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reversed(rows):
        url = str(row.get("source_url") or row.get("url") or "").strip()
        reason = str(row.get("reason") or "unknown")
        host = urlparse(url).netloc.lower()
        sample = {
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "source_url": url,
            "reason": reason,
            "detail": row.get("detail"),
            "http_status": row.get("http_status"),
            "path_shape": path_shape(url),
        }
        if len(samples_by_reason[reason]) < 5:
            samples_by_reason[reason].append(sample)
        if host and len(samples_by_host[host]) < 3:
            samples_by_host[host].append(sample)

    report = {
        "generated_at": utc_now(),
        "failure_rows": len(rows),
        "unique_failed_urls": len(latest_by_url),
        "reason_counts_all_attempts": dict(reasons.most_common()),
        "reason_counts_latest_by_url": dict(latest_reasons.most_common()),
        "http_status_counts": dict(statuses.most_common()),
        "top_hosts": [{"host": host, "count": count} for host, count in hosts.most_common(30)],
        "top_path_shapes": [{"path_shape": shape, "count": count} for shape, count in shapes.most_common(40)],
        "samples_by_reason": dict(samples_by_reason),
        "samples_by_top_host": {
            host: samples_by_host[host]
            for host, _ in hosts.most_common(20)
            if host in samples_by_host
        },
    }
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "detail_failure_diagnostics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "failure_rows": report["failure_rows"],
        "unique_failed_urls": report["unique_failed_urls"],
        "top_reasons": list(reasons.most_common(8)),
        "top_hosts": list(hosts.most_common(8)),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
