#!/usr/bin/env python3
"""Conservative public-career-page crawler for the Company1000 research set.

It never logs in, bypasses CAPTCHA/paywalls, infers salary, or writes
`active_verified`. Dynamic or blocked pages are recorded as auditable coverage
failures for later browser/API work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (compatible; LietoulaCompany1000Research/1.0; "
    "+public-career-page-audit)"
)
JOB_WORDS = re.compile(
    r"招聘|职位|岗位|社会招聘|校园招聘|实习|career|careers|job|jobs|vacancy|position",
    re.I,
)
BLOCK_WORDS = re.compile(r"验证码|captcha|登录后|请登录|access denied|forbidden", re.I)
THIRD_PARTY_HOSTS = {
    "zhaopin.com", "liepin.com", "51job.com", "bosszhipin.com",
    "lagou.com", "linkedin.com", "indeed.com",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_companies(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    companies = data.get("companies") if isinstance(data, dict) else data
    if not isinstance(companies, list):
        raise ValueError("seed must be a list or an object containing companies[]")
    return [x for x in companies if isinstance(x, dict)]


def host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def registrable_hint(domain: str) -> str:
    parts = [p for p in domain.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def same_official_family(url: str, official_site: str, career_url: str) -> bool:
    h = host(url)
    if not h:
        return False
    allowed = {registrable_hint(host(official_site)), registrable_hint(host(career_url))}
    return registrable_hint(h) in allowed or any(h.endswith("." + d) for d in allowed if d)


def canonical(url: str) -> str:
    return urldefrag(url)[0].strip()


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def existing_keys(path: Path, key: str) -> set[str]:
    result: set[str] = set()
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line).get(key)
            if value:
                result.add(str(value))
        except json.JSONDecodeError:
            continue
    return result


@dataclass
class FetchResult:
    url: str
    final_url: str | None
    http_status: int | None
    title: str | None
    text_length: int
    blocked: bool
    javascript_shell: bool
    error: str | None
    job_links: list[str]


def fetch_page(session: requests.Session, url: str, official_site: str, career_url: str) -> FetchResult:
    try:
        response = session.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException as exc:
        return FetchResult(url, None, None, None, 0, False, False, type(exc).__name__, [])

    text = response.text or ""
    soup = BeautifulSoup(text, "lxml")
    visible = " ".join(soup.stripped_strings)
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    blocked = response.status_code in {401, 403, 429} or bool(BLOCK_WORDS.search(visible[:5000]))
    javascript_shell = len(visible) < 120 and (
        "enable javascript" in visible.lower()
        or len(soup.find_all("script")) >= 2
        or "__NEXT_DATA__" in text
    )

    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        label = " ".join(a.stripped_strings)
        href = canonical(urljoin(response.url, a.get("href", "")))
        if not href.startswith(("http://", "https://")):
            continue
        if not JOB_WORDS.search(label + " " + href):
            continue
        h = host(href)
        if any(h == d or h.endswith("." + d) for d in THIRD_PARTY_HOSTS):
            continue
        if not same_official_family(href, official_site, career_url):
            # ATS links are retained only when directly linked by the official page.
            if not any(x in h for x in ("zhiye", "moka", "beisen", "chinahr")):
                continue
        if href not in seen:
            seen.add(href)
            links.append(href)
        if len(links) >= 100:
            break

    return FetchResult(
        url=url,
        final_url=response.url,
        http_status=response.status_code,
        title=title,
        text_length=len(visible),
        blocked=blocked,
        javascript_shell=javascript_shell,
        error=None,
        job_links=links,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="company_seed_1000.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--start-index", type=int)
    args = parser.parse_args()

    seed_path = Path(args.seed_file)
    out_dir = Path(args.output_dir)
    state_path = Path(args.state_file)
    companies = load_companies(seed_path)

    state: dict[str, Any] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    start = args.start_index if args.start_index is not None else int(state.get("next_batch_start_index", 0))
    batch = companies[start : start + max(1, args.batch_size)]

    coverage_file = out_dir / "coverage_auto.jsonl"
    failure_file = out_dir / "failures_auto.jsonl"
    link_file = out_dir / "job_link_candidates_auto.jsonl"
    processed = existing_keys(coverage_file, "audit_key")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})
    coverage_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    link_rows: list[dict[str, Any]] = []

    for company in batch:
        company_id = str(company.get("company_id") or "")
        name = str(company.get("company_name") or company.get("name") or "")
        career_url = str(company.get("career_url") or "")
        official_site = str(company.get("official_website") or career_url)
        audit_key = hashlib.sha256(f"{company_id}|{career_url}".encode()).hexdigest()[:20]
        if audit_key in processed:
            continue
        if not career_url:
            failure_rows.append({
                "company_id": company_id, "company_name": name,
                "reason": "missing_career_url", "observed_at": utc_now(),
            })
            continue

        result = fetch_page(session, career_url, official_site, career_url)
        verified = bool(
            result.http_status and 200 <= result.http_status < 400
            and result.final_url
            and same_official_family(result.final_url, official_site, career_url)
        )
        enumeration_status = "html_links_discovered" if result.job_links else "no_links_observed"
        if result.blocked:
            enumeration_status = "restricted"
        elif result.javascript_shell:
            enumeration_status = "pending_dynamic_js"
        elif result.error:
            enumeration_status = "fetch_failed"

        coverage_rows.append({
            "audit_key": audit_key,
            "company_id": company_id,
            "company_name": name,
            "official_entry_url": career_url,
            "final_url": result.final_url,
            "official_entry_verified": verified,
            "http_status": result.http_status,
            "page_title": result.title,
            "text_length": result.text_length,
            "enumeration_status": enumeration_status,
            "job_link_candidate_count": len(result.job_links),
            "error": result.error,
            "observed_at": utc_now(),
            "final_acceptance_met": False,
        })
        if result.error or result.blocked or result.javascript_shell:
            failure_rows.append({
                "company_id": company_id,
                "company_name": name,
                "url": career_url,
                "reason": enumeration_status,
                "http_status": result.http_status,
                "observed_at": utc_now(),
            })
        for job_url in result.job_links:
            link_rows.append({
                "company_id": company_id,
                "company_name": name,
                "source_url": job_url,
                "review_status": "review_pending",
                "discovered_at": utc_now(),
            })

    append_jsonl(coverage_file, coverage_rows)
    append_jsonl(failure_file, failure_rows)
    append_jsonl(link_file, link_rows)

    next_index = min(len(companies), start + len(batch))
    state.update({
        "updated_at": utc_now(),
        "target_companies": 1000,
        "valid_seed_count": len(companies),
        "next_batch_start_index": next_index,
        "last_auto_batch": {"start_index": start, "count": len(batch)},
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
        },
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"processed": len(coverage_rows), "links": len(link_rows), "next_index": next_index}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
