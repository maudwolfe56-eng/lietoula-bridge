#!/usr/bin/env python3
"""Extract role-level candidates from public multi-position recruitment announcements.

This is a fallback for official announcement pages that do not expose structured
JobPosting data and may place detailed qualifications in images or attachments. It
extracts only role names, headcounts, dates, location and explicitly disclosed salary
from visible text. Missing responsibilities/requirements stay null and the record
remains ``candidate_raw`` (or ``inactive_expired``); no information is invented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

from crawl_company_jobs import BLOCK_WORDS, USER_AGENT, append_jsonl, utc_now
from resolve_job_detail_candidates import CITY_RE, dates_from_text, parse_date

ROOT = Path(__file__).resolve().parent
ROLE_COUNT_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()·/—-]{2,45}?)(\d{1,3})\s*(?:名|人)",
    re.I,
)
ROLE_CONTEXT_RE = re.compile(
    r"(?:公开招聘|招聘|招募)(.{2,220}?)(?:，?具体(?:如下|事宜|要求)|。|；|\n)",
    re.I | re.S,
)
GENERIC_ROLE_PREFIX = re.compile(
    r"^(?:现面向|面向|集团内外部|集团内、外部|社会|校园|内部|外部|公开)+",
)


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


def load_glob(directory: Path, pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        rows.extend(read_jsonl(path))
    return rows


def unique_rows(rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = "|".join(str(row.get(field) or "") for field in fields)
        if key.strip("|"):
            index[key] = row
    return list(index.values())


def clean_role_name(value: str) -> str | None:
    role = re.sub(r"\s+", "", value).strip("，,、；;：:。")
    role = GENERIC_ROLE_PREFIX.sub("", role)
    role = re.sub(r"^(?:招聘|岗位|职位)", "", role)
    role = role.strip("，,、；;：:。")
    if not 2 <= len(role) <= 45:
        return None
    if any(word in role for word in ("报名", "材料", "联系方式", "注意事项", "毕业证", "身份证")):
        return None
    return role


def extract_roles(text: str) -> list[tuple[str, int]]:
    contexts = [match.group(1) for match in ROLE_CONTEXT_RE.finditer(text)]
    if not contexts:
        contexts = [text[:6000]]
    roles: dict[str, int] = {}
    for context in contexts:
        for match in ROLE_COUNT_RE.finditer(context):
            role = clean_role_name(match.group(1))
            if role:
                roles[role] = max(roles.get(role, 0), int(match.group(2)))
    return list(roles.items())[:30]


def extract_deadline(text: str) -> str | None:
    for match in re.finditer(r"(?:报名时间|申请时间|截止时间|报名截止)(.{0,160})", text, re.I):
        dates = dates_from_text(match.group(1))
        if dates:
            return dates[-1]
    dates = dates_from_text(text[:8000])
    return dates[-1] if len(dates) >= 2 else None


def extract_published_date(url: str, text: str) -> str | None:
    match = re.search(r"/(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:/|\.)", url)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    dates = dates_from_text(text[:10000])
    return dates[0] if dates else None


def resolve_announcement(row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_url = str(row.get("source_url") or row.get("url") or "").strip()
    observed_at = utc_now()
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })
    try:
        response = session.get(source_url, timeout=25, allow_redirects=True)
    except requests.RequestException as exc:
        return [], [{
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "source_url": source_url,
            "url": source_url,
            "reason": f"announcement_fetch_failed:{type(exc).__name__}",
            "observed_at": observed_at,
        }]

    soup = BeautifulSoup(response.text or "", "lxml")
    text = soup.get_text("\n", strip=True)
    visible = " ".join(soup.stripped_strings)
    if response.status_code in {401, 403, 429} or BLOCK_WORDS.search(visible[:5000]):
        return [], [{
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "source_url": source_url,
            "url": source_url,
            "http_status": response.status_code,
            "reason": "announcement_access_restricted",
            "observed_at": observed_at,
        }]
    if response.status_code >= 400:
        return [], [{
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "source_url": source_url,
            "url": source_url,
            "http_status": response.status_code,
            "reason": "announcement_http_error",
            "observed_at": observed_at,
        }]

    roles = extract_roles(text)
    if not roles:
        return [], [{
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "source_url": source_url,
            "url": source_url,
            "http_status": response.status_code,
            "reason": "announcement_role_names_not_extractable",
            "observed_at": observed_at,
        }]

    published_date = extract_published_date(response.url, text)
    deadline_date = extract_deadline(text)
    city_match = CITY_RE.search(text[:10000])
    location = city_match.group(0) if city_match else None
    salary = "面议" if re.search(r"薪酬\s*面议|工资\s*面议", text) else None
    expired = bool(
        deadline_date
        and datetime.fromisoformat(deadline_date).date() < datetime.now(timezone.utc).date()
    )

    jobs: list[dict[str, Any]] = []
    for index, (role, headcount) in enumerate(roles):
        suffix = "" if index == 0 else "#role-" + hashlib.sha256(role.encode()).hexdigest()[:10]
        record_url = source_url + suffix
        completeness_checks = [role, location, published_date or deadline_date, record_url]
        completeness = round(sum(bool(value) for value in completeness_checks) / 7, 3)
        status = "inactive_expired" if expired else "candidate_raw"
        jobs.append({
            "record_id": hashlib.sha256(
                f"{row.get('company_id')}|{source_url}|{role}".encode()
            ).hexdigest()[:24],
            "company_id": row.get("company_id"),
            "company_name": row.get("company_name"),
            "company": row.get("company_name"),
            "job_title": role,
            "job_name": role,
            "department": None,
            "responsibilities": None,
            "requirements": None,
            "location": location,
            "city": location,
            "salary": salary,
            "salary_disclosure_status": "disclosed" if salary else "not_disclosed",
            "experience": None,
            "education": None,
            "headcount": headcount,
            "published_date": published_date,
            "deadline_date": deadline_date,
            "source_url": record_url,
            "source_page_url": response.url,
            "source_type": "official_recruitment_announcement",
            "verified_at": observed_at,
            "detail_status": "role_extracted_from_multi_position_announcement_fields_pending",
            "completeness": completeness,
            "confidence": 0.72,
            "review_status": status,
            "review_recommendation": status,
            "active_verified": False,
        })
    return jobs, []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()

    output = ROOT / args.output_dir
    failures = load_glob(output, "*failures_job_details*.jsonl")
    candidates = unique_rows([
        row for row in failures
        if row.get("reason") == "detail_unparseable_or_multi_position_announcement"
        and row.get("source_url")
    ], ("company_id", "source_url"))

    existing_jobs = load_glob(output, "*jobs*.jsonl")
    resolved_pages = {
        str(row.get("source_page_url") or row.get("source_url") or "").split("#", 1)[0]
        for row in existing_jobs
        if row.get("source_page_url") or row.get("source_url")
    }
    prior_failures = load_glob(output, "*failures_announcements*.jsonl")
    attempts: dict[str, int] = {}
    for row in prior_failures:
        url = str(row.get("source_url") or row.get("url") or "")
        attempts[url] = attempts.get(url, 0) + 1

    pending = [
        row for row in candidates
        if str(row.get("source_url") or "").split("#", 1)[0] not in resolved_pages
        and attempts.get(str(row.get("source_url") or ""), 0) < max(1, args.max_attempts)
    ][: max(1, args.batch_size)]

    jobs: list[dict[str, Any]] = []
    new_failures: list[dict[str, Any]] = []
    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(resolve_announcement, pending))
        for job_rows, failure_rows in results:
            jobs.extend(job_rows)
            new_failures.extend(failure_rows)

    append_jsonl(output / "jobs_announcement_roles_auto.jsonl", jobs)
    append_jsonl(output / "failures_announcements_auto.jsonl", new_failures)
    state = {
        "updated_at": utc_now(),
        "announcement_candidates": len(candidates),
        "selected": len(pending),
        "role_records_created": len(jobs),
        "explicit_failures_created": len(new_failures),
        "policy": {
            "auto_active_verified": False,
            "missing_fields_inferred": False,
            "salary_inference_allowed": False,
            "access_control_bypass_allowed": False,
        },
    }
    runtime = ROOT / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "announcement_resolution_checkpoint.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
