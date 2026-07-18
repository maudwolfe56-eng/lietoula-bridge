#!/usr/bin/env python3
"""Build auditable CompanySource, JobImport, Coverage, Failures, JobChanges and run_summary outputs.

The builder is deliberately conservative: it never infers salary, never marks a job
active_verified, and only recommends review_pending when the official-source record
contains the minimum fields required for human review.
"""
from __future__ import annotations

import csv
import glob
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
RUNTIME = ROOT / "runtime"
DELIVERABLES = ROOT / "deliverables"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def load_seed_rows() -> list[dict[str, Any]]:
    merged = RUNTIME / "company_seed_merged.json"
    if merged.exists():
        data = read_json(merged)
        rows = data.get("companies", []) if isinstance(data, dict) else data
        return [row for row in rows if isinstance(row, dict)]

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    paths = [ROOT / "company_seed_1000.json", *sorted(ROOT.glob("company_seed_expansion_*.json"))]
    for path in paths:
        if not path.exists():
            continue
        data = read_json(path)
        items = data.get("companies", []) if isinstance(data, dict) else data
        for row in items:
            if not isinstance(row, dict):
                continue
            company_id = str(row.get("company_id") or "")
            if not company_id or company_id in seen:
                continue
            seen.add(company_id)
            rows.append(row)
    return rows


def load_company_source_overrides() -> dict[str, dict[str, Any]]:
    """Load audited CompanySource updates from timestamped handoff packages.

    Seed files are discovery inputs. The weekly_update packages contain later, manually
    verified official URLs, evidence and per-company acceptance outcomes. Process them
    chronologically so the newest non-null value wins and aggregate rebuilds do not
    regress verified records back to seed candidates.
    """
    overrides: dict[str, dict[str, Any]] = {}
    for path in sorted(DELIVERABLES.glob("weekly_update_*.json")):
        try:
            data = read_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        updates = data.get("company_source_updates", []) if isinstance(data, dict) else []
        for row in updates:
            if not isinstance(row, dict):
                continue
            company_id = str(row.get("company_id") or "").strip()
            if not company_id:
                continue
            target = overrides.setdefault(company_id, {})
            for key, value in row.items():
                if value is not None:
                    target[key] = value
    return overrides


def load_job_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pattern in ("jobs*.jsonl", "*jobs*.jsonl"):
        for name in sorted(glob.glob(str(OUTPUT / pattern))):
            rows.extend(read_jsonl(Path(name)))
    # A small number of historical result files are JSON objects containing jobs[].
    for path in sorted((ROOT / "results").glob("*.json")) if (ROOT / "results").exists() else []:
        try:
            data = read_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        items = data.get("jobs", []) if isinstance(data, dict) else []
        rows.extend(row for row in items if isinstance(row, dict))
    return rows


def dedup(rows: Iterable[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for row in rows:
        key = "|".join(str(row.get(field) or "") for field in key_fields)
        if not key.strip("|"):
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key in index:
            output[index[key]] = row
        else:
            index[key] = len(output)
            output.append(row)
    return output


def official_source(url: str | None) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    disallowed = ("liepin.com", "51job.com", "bosszhipin.com", "lagou.com", "indeed.com", "linkedin.com")
    return not any(host == domain or host.endswith("." + domain) for domain in disallowed)


def normalize_job(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item.setdefault("company", item.get("company_name"))
    item.setdefault("job_name", item.get("job_title"))
    item.setdefault("source_type", "official_recruitment_portal")
    item.setdefault("verified_at", item.get("observed_at") or utc_now())
    item["active_verified"] = False

    if not item.get("salary"):
        item["salary"] = None
        item["salary_disclosure_status"] = "not_disclosed"

    checks = {
        "company": bool(item.get("company_name") or item.get("company")),
        "title": bool(item.get("job_title") or item.get("job_name")),
        "responsibilities": bool(item.get("responsibilities")),
        "requirements": bool(item.get("requirements")),
        "location": bool(item.get("city") or item.get("location")),
        "source_url": bool(item.get("source_url")),
        "freshness": bool(item.get("published_date") or item.get("deadline_date")),
    }
    score = round(sum(checks.values()) / len(checks), 3)
    item["completeness"] = score

    source_ok = official_source(item.get("source_url"))
    confidence = 0.45 + 0.35 * score + (0.15 if source_ok else 0.0)
    item["confidence"] = round(min(confidence, 0.95), 3)

    prior = str(item.get("review_status") or "candidate_raw")
    if prior == "inactive_expired":
        recommendation = "inactive_expired"
    elif source_ok and checks["location"] and checks["responsibilities"] and checks["requirements"] and checks["freshness"] and score >= 0.70:
        recommendation = "review_pending"
    else:
        recommendation = "candidate_raw"
    item["review_status"] = recommendation
    item["review_recommendation"] = recommendation
    return item


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def main() -> int:
    DELIVERABLES.mkdir(parents=True, exist_ok=True)
    seed_rows = load_seed_rows()
    company_source_overrides = load_company_source_overrides()
    company_sources: list[dict[str, Any]] = []
    seen_company_ids: set[str] = set()
    for row in seed_rows:
        item = dict(row)
        company_id = str(item.get("company_id") or "").strip()
        if company_id:
            seen_company_ids.add(company_id)
            item.update(company_source_overrides.get(company_id, {}))
        item["source_type"] = item.get("source_type") or "official_company_or_recruitment_portal"
        item["verified_at"] = item.get("verified_at") or utc_now()
        item["final_acceptance_met"] = bool(item.get("final_acceptance_met", False))
        company_sources.append(item)

    # Preserve a verified handoff even if a malformed/late seed merge temporarily omits it.
    for company_id, override in company_source_overrides.items():
        if company_id in seen_company_ids:
            continue
        item = dict(override)
        item["company_id"] = company_id
        item["source_type"] = item.get("source_type") or "official_company_or_recruitment_portal"
        item["verified_at"] = item.get("verified_at") or utc_now()
        item["final_acceptance_met"] = bool(item.get("final_acceptance_met", False))
        company_sources.append(item)

    raw_jobs = load_job_rows()
    normalized_jobs = [normalize_job(row) for row in raw_jobs]
    jobs = dedup(normalized_jobs, ("record_id", "source_url", "company_id", "job_code", "job_title"))

    coverage_rows: list[dict[str, Any]] = []
    for path in sorted(OUTPUT.glob("coverage*.jsonl")):
        coverage_rows.extend(read_jsonl(path))
    coverage = dedup(coverage_rows, ("audit_key", "company_id", "official_entry_url"))

    failure_rows: list[dict[str, Any]] = []
    for path in [ROOT / "failures.ndjson", *sorted(OUTPUT.glob("failures*.jsonl"))]:
        failure_rows.extend(read_jsonl(path))
    failures = dedup(failure_rows, ("company_id", "url", "reason", "http_status"))

    changes = []
    for job in jobs:
        changes.append({
            "record_id": job.get("record_id"),
            "company_id": job.get("company_id"),
            "job_title": job.get("job_title") or job.get("job_name"),
            "source_url": job.get("source_url"),
            "change_type": "first_seen_snapshot",
            "detected_at": job.get("observed_at") or utc_now(),
            "review_status": job.get("review_status"),
        })

    for name, rows in (
        ("CompanySource", company_sources),
        ("JobImport", jobs),
        ("Coverage", coverage),
        ("Failures", failures),
        ("JobChanges", changes),
    ):
        write_jsonl(DELIVERABLES / f"{name}.jsonl", rows)
        write_csv(DELIVERABLES / f"{name}.csv", rows)

    checkpoint = read_json(RUNTIME / "checkpoint.json") if (RUNTIME / "checkpoint.json").exists() else {}
    status_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("review_status") or "candidate_raw")
        status_counts[status] = status_counts.get(status, 0) + 1
    company_source_acceptance_count = sum(1 for row in company_sources if row.get("final_acceptance_met") is True)
    summary = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "target_companies": 1000,
        "seeded_companies": len(company_sources),
        "coverage_records": len(coverage),
        "companies_with_coverage": len({row.get("company_id") for row in coverage if row.get("company_id")}),
        "company_source_final_acceptance_count": company_source_acceptance_count,
        "job_records": len(jobs),
        "job_status_counts": status_counts,
        "failure_records": len(failures),
        "job_change_records": len(changes),
        "active_verified_records": 0,
        "final_acceptance_met": False,
        "checkpoint": checkpoint,
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
        },
    }
    (DELIVERABLES / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
