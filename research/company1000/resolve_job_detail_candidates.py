#!/usr/bin/env python3
"""Resolve probable official job-detail candidates into conservative job records.

Only publicly accessible pages are fetched. The resolver never logs in, solves a
CAPTCHA, bypasses access controls, or infers undisclosed salary. Schema.org
``JobPosting`` is preferred; a bounded HTML-section parser is used only when the
page clearly represents one position. Ambiguous announcements and navigation
pages become explicit failures instead of fabricated jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from crawl_company_jobs import (
    BLOCK_WORDS,
    USER_AGENT,
    append_jsonl,
    is_probable_job_detail_url,
    utc_now,
)

ROOT = Path(__file__).resolve().parent
GENERIC_TITLE = re.compile(
    r"^(?:招聘|招聘信息|招聘公告|校园招聘|社会招聘|职位列表|加入我们|人才招聘|"
    r"careers?|jobs?|job search|search jobs?)$",
    re.I,
)
MULTI_ROLE_TITLE = re.compile(r"招聘公告|招聘启事|招聘简章|校园招聘|社会招聘|批量招聘|若干岗位", re.I)
RESP_HEADINGS = r"岗位职责|工作职责|职位职责|职责描述|职位描述|工作内容|主要职责|responsibilities|job description|what you(?:'|’)ll do"
REQ_HEADINGS = r"任职要求|岗位要求|职位要求|资格条件|任职资格|应聘条件|任职条件|qualifications|requirements|what you bring"
STOP_HEADINGS = r"薪酬福利|福利待遇|工作地点|办公地点|申请方式|报名方式|截止日期|联系方式|benefits|location|how to apply"
CITY_RE = re.compile(
    r"北京|上海|天津|重庆|深圳|广州|杭州|南京|苏州|成都|武汉|西安|长沙|郑州|青岛|厦门|"
    r"宁波|无锡|合肥|福州|济南|大连|沈阳|长春|哈尔滨|石家庄|太原|南昌|南宁|海口|"
    r"昆明|贵阳|兰州|西宁|银川|乌鲁木齐|呼和浩特|香港|澳门|珠海|佛山|东莞|惠州|"
    r"常州|南通|嘉兴|绍兴|温州|台州|泉州|烟台|潍坊|洛阳|海外"
)
DATE_RE = re.compile(r"(?:20\d{2})[年./-](?:0?[1-9]|1[0-2])[月./-](?:0?[1-9]|[12]\d|3[01])日?")


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
        if not key.strip("|"):
            continue
        index[key] = row
    return list(index.values())


def flatten_json_ld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "itemListElement"):
            child = value.get(key)
            if child is not None:
                yield from flatten_json_ld(child)
    elif isinstance(value, list):
        for item in value:
            yield from flatten_json_ld(item)


def job_postings(soup: BeautifulSoup) -> list[dict[str, Any]]:
    postings: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\+json", re.I)}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in flatten_json_ld(data):
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(value).lower() == "jobposting" for value in kinds if value):
                postings.append(item)
    return postings


def text_from_html(value: Any) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(str(value), "lxml").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def address_from_job_location(value: Any) -> str | None:
    locations = value if isinstance(value, list) else [value]
    parts: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if not isinstance(address, dict):
            continue
        for key in ("addressCountry", "addressRegion", "addressLocality", "streetAddress"):
            field = address.get(key)
            if isinstance(field, dict):
                field = field.get("name")
            if field and str(field) not in parts:
                parts.append(str(field))
    return " ".join(parts) or None


def stringify_requirement(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text.strip() or None


def salary_from_schema(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    currency = value.get("currency") or ""
    unit = ""
    raw = value.get("value")
    if isinstance(raw, dict):
        minimum = raw.get("minValue")
        maximum = raw.get("maxValue")
        unit = raw.get("unitText") or ""
        if minimum is not None and maximum is not None:
            amount = f"{minimum}-{maximum}"
        else:
            amount = str(raw.get("value") or minimum or maximum or "")
    else:
        amount = str(raw or "")
    result = " ".join(part for part in (currency, amount, unit) if part).strip()
    return result or None


def parse_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value), fuzzy=False)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed.date().isoformat()


def extract_section(text: str, heading_pattern: str, stop_pattern: str) -> str | None:
    pattern = re.compile(
        rf"(?:^|\n|\r|\s)(?:{heading_pattern})\s*[:：]?\s*(.+?)"
        rf"(?=(?:\n|\r|\s)(?:{stop_pattern})\s*[:：]?|$)",
        re.I | re.S,
    )
    match = pattern.search(text)
    if not match:
        return None
    result = re.sub(r"\s+", " ", match.group(1)).strip(" ：:;；")
    return result[:6000] if len(result) >= 12 else None


def first_labeled_value(text: str, labels: str, limit: int = 180) -> str | None:
    match = re.search(rf"(?:{labels})\s*[:：]\s*([^\n\r；;]{{1,{limit}}})", text, re.I)
    return match.group(1).strip() if match else None


def dates_from_text(text: str) -> list[str]:
    result: list[str] = []
    for raw in DATE_RE.findall(text):
        normalized = parse_date(raw.replace("年", "-").replace("月", "-").replace("日", ""))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def title_from_page(soup: BeautifulSoup) -> str | None:
    for selector in ("h1", "[class*='job-title']", "[class*='position-title']", "h2"):
        node = soup.select_one(selector)
        if node:
            title = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if 2 <= len(title) <= 180 and not GENERIC_TITLE.match(title):
                return title
    if soup.title:
        title = re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True)).strip()
        title = re.split(r"\s[-_|｜]\s", title)[0].strip()
        if 2 <= len(title) <= 180 and not GENERIC_TITLE.match(title):
            return title
    return None


def parse_schema_posting(
    posting: dict[str, Any],
    candidate: dict[str, Any],
    source_url: str,
    observed_at: str,
) -> dict[str, Any] | None:
    title = str(posting.get("title") or posting.get("name") or "").strip()
    if not title or GENERIC_TITLE.match(title):
        return None
    description = text_from_html(posting.get("description"))
    responsibilities = text_from_html(posting.get("responsibilities"))
    requirements = text_from_html(posting.get("qualifications")) or stringify_requirement(
        posting.get("skills") or posting.get("experienceRequirements")
    )
    if description:
        responsibilities = responsibilities or extract_section(description, RESP_HEADINGS, REQ_HEADINGS + "|" + STOP_HEADINGS)
        requirements = requirements or extract_section(description, REQ_HEADINGS, STOP_HEADINGS)
    location = address_from_job_location(posting.get("jobLocation"))
    salary = salary_from_schema(posting.get("baseSalary") or posting.get("estimatedSalary"))
    company_name = candidate.get("company_name")
    organization = posting.get("hiringOrganization")
    if isinstance(organization, dict) and organization.get("name"):
        company_name = company_name or organization.get("name")

    return {
        "record_id": hashlib.sha256(f"{candidate.get('company_id')}|{source_url}|{title}".encode()).hexdigest()[:24],
        "company_id": candidate.get("company_id"),
        "company_name": company_name,
        "company": company_name,
        "job_title": title,
        "job_name": title,
        "department": posting.get("occupationalCategory"),
        "responsibilities": responsibilities,
        "requirements": requirements,
        "location": location,
        "city": location,
        "salary": salary,
        "salary_disclosure_status": "disclosed" if salary else "not_disclosed",
        "experience": stringify_requirement(posting.get("experienceRequirements")),
        "education": stringify_requirement(posting.get("educationRequirements")),
        "published_date": parse_date(posting.get("datePosted")),
        "deadline_date": parse_date(posting.get("validThrough")),
        "employment_type": stringify_requirement(posting.get("employmentType")),
        "source_url": source_url,
        "source_type": "official_recruitment_portal",
        "verified_at": observed_at,
        "detail_status": "parsed_schema_org_jobposting",
        "active_verified": False,
    }


def parse_html_posting(
    soup: BeautifulSoup,
    candidate: dict[str, Any],
    source_url: str,
    observed_at: str,
) -> dict[str, Any] | None:
    title = title_from_page(soup)
    if not title or MULTI_ROLE_TITLE.search(title):
        return None
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    responsibilities = extract_section(text, RESP_HEADINGS, REQ_HEADINGS + "|" + STOP_HEADINGS)
    requirements = extract_section(text, REQ_HEADINGS, STOP_HEADINGS)
    if not responsibilities and not requirements:
        return None

    location = first_labeled_value(text, r"工作地点|办公地点|工作城市|location")
    if not location:
        city = CITY_RE.search(text[:4000])
        location = city.group(0) if city else None
    dates = dates_from_text(text)
    published = first_labeled_value(text, r"发布日期|发布时间|发布于|date posted", 40)
    deadline = first_labeled_value(text, r"截止日期|报名截止|申请截止|valid through", 40)
    published_date = parse_date(published) or (dates[0] if dates else None)
    deadline_date = parse_date(deadline) or (dates[-1] if len(dates) >= 2 else None)
    salary = first_labeled_value(text, r"薪资|薪酬|工资|月薪|年薪|salary", 80)
    experience = first_labeled_value(text, r"工作经验|经验要求|经验|experience", 120)
    education = first_labeled_value(text, r"学历要求|学历|education", 120)
    department = first_labeled_value(text, r"招聘部门|所属部门|部门|department", 120)

    company_name = candidate.get("company_name")
    return {
        "record_id": hashlib.sha256(f"{candidate.get('company_id')}|{source_url}|{title}".encode()).hexdigest()[:24],
        "company_id": candidate.get("company_id"),
        "company_name": company_name,
        "company": company_name,
        "job_title": title,
        "job_name": title,
        "department": department,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "location": location,
        "city": location,
        "salary": salary,
        "salary_disclosure_status": "disclosed" if salary else "not_disclosed",
        "experience": experience,
        "education": education,
        "published_date": published_date,
        "deadline_date": deadline_date,
        "source_url": source_url,
        "source_type": "official_recruitment_portal",
        "verified_at": observed_at,
        "detail_status": "parsed_single_position_html",
        "active_verified": False,
    }


def finalize_status(job: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "company": bool(job.get("company_id") or job.get("company_name")),
        "title": bool(job.get("job_title")),
        "responsibilities": bool(job.get("responsibilities")),
        "requirements": bool(job.get("requirements")),
        "location": bool(job.get("location")),
        "source_url": bool(job.get("source_url")),
        "freshness": bool(job.get("published_date") or job.get("deadline_date")),
    }
    completeness = round(sum(checks.values()) / len(checks), 3)
    job["completeness"] = completeness
    job["confidence"] = round(min(0.95, 0.55 + 0.35 * completeness), 3)
    deadline = parse_date(job.get("deadline_date"))
    if deadline and date.fromisoformat(deadline) < datetime.now(timezone.utc).date():
        status = "inactive_expired"
    elif all(checks[key] for key in ("company", "title", "responsibilities", "requirements", "location", "source_url", "freshness")) and completeness >= 0.70:
        status = "review_pending"
    else:
        status = "candidate_raw"
    job["review_status"] = status
    job["review_recommendation"] = status
    job["active_verified"] = False
    return job


def resolve_one(candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_url = str(candidate.get("source_url") or "").strip()
    observed_at = utc_now()
    if not source_url or not is_probable_job_detail_url(source_url):
        return [], [{
            "company_id": candidate.get("company_id"),
            "company_name": candidate.get("company_name"),
            "url": source_url or None,
            "source_url": source_url or None,
            "reason": "not_probable_individual_job_detail",
            "observed_at": observed_at,
        }]

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })
    try:
        response = session.get(source_url, timeout=25, allow_redirects=True)
    except requests.RequestException as exc:
        return [], [{
            "company_id": candidate.get("company_id"),
            "company_name": candidate.get("company_name"),
            "url": source_url,
            "source_url": source_url,
            "reason": f"detail_fetch_failed:{type(exc).__name__}",
            "observed_at": observed_at,
        }]

    text = response.text or ""
    soup = BeautifulSoup(text, "lxml")
    visible = " ".join(soup.stripped_strings)
    blocked = response.status_code in {401, 403, 429} or bool(BLOCK_WORDS.search(visible[:5000]))
    if blocked:
        return [], [{
            "company_id": candidate.get("company_id"),
            "company_name": candidate.get("company_name"),
            "url": source_url,
            "source_url": source_url,
            "final_url": response.url,
            "http_status": response.status_code,
            "reason": "detail_access_restricted",
            "observed_at": observed_at,
        }]
    if response.status_code >= 400:
        return [], [{
            "company_id": candidate.get("company_id"),
            "company_name": candidate.get("company_name"),
            "url": source_url,
            "source_url": source_url,
            "final_url": response.url,
            "http_status": response.status_code,
            "reason": "detail_http_error",
            "observed_at": observed_at,
        }]

    jobs: list[dict[str, Any]] = []
    for posting in job_postings(soup):
        job = parse_schema_posting(posting, candidate, response.url, observed_at)
        if job:
            jobs.append(finalize_status(job))
    if not jobs:
        job = parse_html_posting(soup, candidate, response.url, observed_at)
        if job:
            jobs.append(finalize_status(job))
    if jobs:
        return jobs, []

    return [], [{
        "company_id": candidate.get("company_id"),
        "company_name": candidate.get("company_name"),
        "url": source_url,
        "source_url": source_url,
        "final_url": response.url,
        "http_status": response.status_code,
        "reason": "detail_unparseable_or_multi_position_announcement",
        "observed_at": observed_at,
    }]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-workers", type=int, default=6)
    args = parser.parse_args()

    output = ROOT / args.output_dir
    candidates = unique_rows(load_glob(output, "*job_link_candidates*.jsonl"), ("company_id", "source_url"))
    candidates = [row for row in candidates if is_probable_job_detail_url(str(row.get("source_url") or ""))]

    existing_jobs = load_glob(output, "*jobs*.jsonl")
    existing_failures = load_glob(output, "*failures*.jsonl")
    resolved_urls = {
        str(row.get("source_url") or "").strip()
        for row in existing_jobs
        if row.get("source_url")
    }
    resolved_urls.update(
        str(row.get("source_url") or row.get("url") or "").strip()
        for row in existing_failures
        if row.get("source_url") or row.get("url")
    )
    pending = [
        row for row in candidates
        if str(row.get("source_url") or "").strip() not in resolved_urls
    ][: max(1, args.batch_size)]

    job_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(resolve_one, pending))
        for jobs, failures in results:
            job_rows.extend(jobs)
            failure_rows.extend(failures)

    append_jsonl(output / "jobs_resolved_auto.jsonl", job_rows)
    append_jsonl(output / "failures_job_details_auto.jsonl", failure_rows)
    state = {
        "updated_at": utc_now(),
        "probable_detail_candidates": len(candidates),
        "selected": len(pending),
        "job_records_created": len(job_rows),
        "explicit_failures_created": len(failure_rows),
        "remaining_unresolved_estimate": max(0, len(candidates) - len(resolved_urls) - len(pending)),
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "access_control_bypass_allowed": False,
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
