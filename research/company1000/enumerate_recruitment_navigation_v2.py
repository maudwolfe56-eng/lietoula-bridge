#!/usr/bin/env python3
"""Bounded official recruitment-navigation enumerator.

This version avoids treating every link on a ``careers.*`` host as recruitment content.
It keeps anchor labels while classifying links, recognizes likely job-detail pages, limits
child-navigation fan-out, and records explicit terminal reasons without bypassing access
controls. It never logs in, solves CAPTCHA, infers salary, or promotes a record beyond
``candidate_raw``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from crawl_company_jobs import (
    ATS_HOST_HINTS,
    BLOCK_WORDS,
    DETAIL_QUERY_KEYS,
    JOB_WORDS,
    THIRD_PARTY_HOSTS,
    USER_AGENT,
    append_jsonl,
    host,
    is_probable_job_detail_url,
    load_companies,
    same_official_family,
    utc_now,
)

ACCEPTED_RESOLUTION_STATUSES = {
    "enumerated",
    "no_current_openings_observed",
    "superseded",
    "not_job_navigation",
    "restricted_with_explicit_reason",
}

NAVIGATION_WORDS = re.compile(
    r"招聘|职位|岗位|人才|加入我们|加入|校招|社招|实习|应聘|"
    r"career|careers|job|jobs|vacancy|vacancies|position|positions|"
    r"recruit|recruitment|talent|join[-_ ]?us|work[-_ ]?with[-_ ]?us|"
    r"opportunit(?:y|ies)|opening|openings|campus|graduate|intern|search",
    re.I,
)
ROLE_TITLE_WORDS = re.compile(
    r"工程师|经理|总监|专员|主管|顾问|研究员|科学家|技术员|分析师|"
    r"设计师|产品|运营|销售|财务|人力|法务|审计|风控|投资|采购|"
    r"engineer|manager|director|specialist|consultant|developer|scientist|"
    r"designer|analyst|associate|intern|officer|lead|architect",
    re.I,
)
GENERIC_NAV_LABELS = re.compile(
    r"^(首页|关于我们|公司介绍|新闻|联系我们|隐私|法律声明|返回|上一页|下一页|"
    r"home|about|news|contact|privacy|legal|back|previous|next|more)$",
    re.I,
)
DETAIL_CONTENT_WORDS = re.compile(
    r"岗位职责|职位职责|工作职责|任职要求|任职资格|职位要求|岗位要求|"
    r"工作地点|招聘人数|薪酬福利|岗位描述|职位描述|"
    r"responsibilit(?:y|ies)|qualification|requirements?|job description|location",
    re.I,
)
NO_OPENINGS_WORDS = re.compile(
    r"暂无(?:招聘|职位|岗位)|暂无空缺|目前没有(?:开放|合适)职位|"
    r"no current openings|no open positions|no vacancies|there are no jobs",
    re.I,
)
HOST_NAV_HINTS = ("career", "careers", "job", "jobs", "recruit", "talent")
TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "ref", "referer", "tracking", "track",
}


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
        if "resolution" in path.name or "filtered" in path.name:
            continue
        rows.extend(read_jsonl(path))
    return rows


def canonical_url(url: str) -> str:
    return urldefrag(url)[0].strip()


def nav_key(company_id: str, source_url: str) -> str:
    return hashlib.sha256(f"{company_id}|{canonical_url(source_url)}".encode()).hexdigest()[:20]


def normalized_depth(row: dict[str, Any]) -> int:
    raw = row.get("navigation_depth")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 1 if row.get("parent_navigation_url") else 0


def canonical_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        company_id = str(row.get("company_id") or "").strip()
        source_url = canonical_url(str(row.get("source_url") or row.get("url") or ""))
        if not company_id or not source_url.startswith(("http://", "https://")):
            continue
        key = nav_key(company_id, source_url)
        normalized = dict(row)
        normalized.update({
            "company_id": company_id,
            "source_url": source_url,
            "source_type": "official_recruitment_navigation",
            "navigation_key": key,
            "navigation_depth": normalized_depth(row),
        })
        prior = unique.get(key)
        if prior is None or normalized_depth(normalized) < normalized_depth(prior):
            unique[key] = normalized
    return list(unique.values())


def historical_navigation_candidates(output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in load_glob(output_dir, "*job_link_candidates*.jsonl"):
        source_url = str(row.get("source_url") or "").strip()
        source_type = str(row.get("source_type") or "")
        if source_type == "official_job_detail_candidate" and is_probable_job_detail_url(source_url):
            continue
        if source_type == "official_recruitment_navigation" or not is_probable_job_detail_url(source_url):
            normalized = dict(row)
            normalized["source_type"] = "official_recruitment_navigation"
            normalized["enumeration_status"] = "historical_candidate_reclassified"
            rows.append(normalized)
    return rows


def url_has_navigation_signal(url: str) -> bool:
    parsed = urlparse(url)
    path_query = f"{parsed.path} {parsed.query}".replace("-", " ").replace("_", " ")
    if NAVIGATION_WORDS.search(path_query):
        return True
    if parsed.path in {"", "/"} and any(hint in parsed.netloc.lower() for hint in HOST_NAV_HINTS):
        return True
    return False


def looks_like_role_link(url: str, label: str) -> bool:
    if not label or GENERIC_NAV_LABELS.search(label.strip()):
        return False
    parsed = urlparse(url)
    query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    has_identifier = bool(DETAIL_QUERY_KEYS.intersection(query)) or bool(
        re.search(r"(?:/|=)(?:[a-z]*\d{4,}[a-z0-9_-]*)(?:/|$|&|\.)", f"{parsed.path}?{parsed.query}", re.I)
    )
    return has_identifier and bool(ROLE_TITLE_WORDS.search(label))


def page_looks_like_detail(title: str, visible: str, source_url: str) -> bool:
    if is_probable_job_detail_url(source_url, title):
        return True
    signals = DETAIL_CONTENT_WORDS.findall(visible[:30000])
    return len(set(x.lower() for x in signals)) >= 2 and bool(
        ROLE_TITLE_WORDS.search(title) or ROLE_TITLE_WORDS.search(visible[:500])
    )


def link_is_allowed(url: str, official_site: str, career_url: str) -> bool:
    target_host = host(url)
    if not target_host:
        return False
    if any(target_host == domain or target_host.endswith("." + domain) for domain in THIRD_PARTY_HOSTS):
        return False
    if same_official_family(url, official_site, career_url):
        return True
    return any(hint in target_host for hint in ATS_HOST_HINTS)


def audit_navigation(
    row: dict[str, Any],
    company_by_id: dict[str, dict[str, Any]],
    max_children: int,
    timeout_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    company_id = str(row.get("company_id") or "")
    company = company_by_id.get(company_id, {})
    company_name = str(row.get("company_name") or company.get("company_name") or "")
    source_url = str(row.get("source_url") or "")
    official_site = str(company.get("official_website") or source_url)
    career_url = str(company.get("career_url") or source_url)
    depth = normalized_depth(row)
    observed_at = utc_now()

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    })

    final_url: str | None = None
    http_status: int | None = None
    title = ""
    visible = ""
    error: str | None = None
    blocked = False
    javascript_shell = False
    detail_links: list[tuple[str, str]] = []
    child_links: list[tuple[str, str]] = []
    filtered_link_count = 0

    try:
        response = session.get(source_url, timeout=timeout_seconds, allow_redirects=True)
        final_url = response.url
        http_status = response.status_code
        html = response.text or ""
        soup = BeautifulSoup(html, "lxml")
        visible = " ".join(soup.stripped_strings)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        blocked = response.status_code in {401, 403, 429} or bool(BLOCK_WORDS.search(visible[:5000]))
        javascript_shell = len(visible) < 120 and (
            "enable javascript" in visible.lower()
            or len(soup.find_all("script")) >= 2
            or "__NEXT_DATA__" in html
        )

        seen_details: set[str] = set()
        seen_children: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            label = " ".join(anchor.stripped_strings).strip()
            href = canonical_url(urljoin(response.url, anchor.get("href", "")))
            if not href.startswith(("http://", "https://")) or href == source_url:
                continue
            if not link_is_allowed(href, official_site, career_url):
                continue

            if is_probable_job_detail_url(href, label) or looks_like_role_link(href, label):
                if href not in seen_details:
                    seen_details.add(href)
                    detail_links.append((href, label))
                continue

            signal_text = f"{label} {urlparse(href).path} {urlparse(href).query}"
            if NAVIGATION_WORDS.search(signal_text) or url_has_navigation_signal(href):
                if href not in seen_children:
                    seen_children.add(href)
                    child_links.append((href, label))
            else:
                filtered_link_count += 1

        detail_links = detail_links[:150]
        child_links = child_links[: max(1, max_children)]
    except requests.RequestException as exc:
        error = type(exc).__name__

    self_is_detail = bool(not error and not blocked and page_looks_like_detail(title, visible, final_url or source_url))
    no_openings = bool(not error and not blocked and NO_OPENINGS_WORDS.search(visible[:30000]))

    if http_status in {404, 410}:
        resolution_status = "superseded"
        reason = f"http_{http_status}"
    elif blocked:
        resolution_status = "restricted_with_explicit_reason"
        reason = f"http_{http_status}_or_access_control_text"
    elif error:
        resolution_status = "restricted_with_explicit_reason"
        reason = error
    elif self_is_detail or detail_links or child_links:
        resolution_status = "enumerated"
        reason = "current_page_looks_like_job_detail" if self_is_detail else None
    elif no_openings:
        resolution_status = "no_current_openings_observed"
        reason = "explicit_no_openings_text_observed"
    elif javascript_shell:
        resolution_status = "requires_site_specific_dynamic_enumerator"
        reason = "javascript_shell_no_public_html_job_enumeration"
    elif not url_has_navigation_signal(source_url) and depth > 0:
        resolution_status = "not_job_navigation"
        reason = "child_url_has_no_recruitment_signal_and_page_has_no_job_evidence"
    else:
        resolution_status = "requires_manual_review_no_links"
        reason = "public_page_reached_but_no_enumerable_links_or_explicit_no_openings_text_observed"

    details: list[dict[str, Any]] = []
    if self_is_detail:
        details.append({
            "company_id": company_id,
            "company_name": company_name,
            "source_url": final_url or source_url,
            "source_type": "official_job_detail_candidate",
            "review_status": "candidate_raw",
            "promotion_eligible": False,
            "review_recommendation": "fetch_detail_and_validate_required_fields",
            "discovered_via": source_url,
            "discovered_at": observed_at,
        })
    for url, label in detail_links:
        details.append({
            "company_id": company_id,
            "company_name": company_name,
            "source_url": url,
            "source_type": "official_job_detail_candidate",
            "link_label": label or None,
            "review_status": "candidate_raw",
            "promotion_eligible": False,
            "review_recommendation": "fetch_detail_and_validate_required_fields",
            "discovered_via": source_url,
            "discovered_at": observed_at,
        })

    children = [{
        "company_id": company_id,
        "company_name": company_name,
        "source_url": url,
        "source_type": "official_recruitment_navigation",
        "link_label": label or None,
        "enumeration_status": "requires_navigation_expansion",
        "parent_navigation_url": source_url,
        "navigation_depth": depth + 1,
        "discovered_at": observed_at,
    } for url, label in child_links]

    resolution = {
        "navigation_key": nav_key(company_id, source_url),
        "company_id": company_id,
        "company_name": company_name,
        "source_url": source_url,
        "final_url": final_url,
        "http_status": http_status,
        "page_title": title or None,
        "text_length": len(visible),
        "navigation_depth": depth,
        "resolution_status": resolution_status,
        "reason": reason,
        "self_detail_candidate": self_is_detail,
        "job_detail_candidates_discovered": len(details),
        "child_navigation_discovered": len(children),
        "filtered_non_recruitment_links": filtered_link_count,
        "observed_at": observed_at,
    }

    failures: list[dict[str, Any]] = []
    if resolution_status not in ACCEPTED_RESOLUTION_STATUSES:
        failures.append({
            "company_id": company_id,
            "company_name": company_name,
            "url": source_url,
            "source_url": source_url,
            "reason": resolution_status,
            "detail": reason,
            "http_status": http_status,
            "observed_at": observed_at,
        })
    return resolution, details, children, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-file", default="runtime/company_seed_merged.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-file", default="runtime/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-children-per-page", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=18)
    args = parser.parse_args()

    seed_rows = load_companies(Path(args.seed_file))
    company_by_id = {str(row.get("company_id") or ""): row for row in seed_rows}
    output_dir = Path(args.output_dir)
    state_path = Path(args.state_file)

    explicit_navigation = load_glob(output_dir, "*recruitment_navigation*.jsonl")
    legacy_navigation = historical_navigation_candidates(output_dir)
    candidates = canonical_candidates([*explicit_navigation, *legacy_navigation])
    resolution_file = output_dir / "recruitment_navigation_resolution_auto.jsonl"
    prior_resolutions = read_jsonl(resolution_file)
    accepted = {
        nav_key(str(row.get("company_id") or ""), str(row.get("source_url") or row.get("url") or ""))
        for row in prior_resolutions
        if str(row.get("resolution_status") or "") in ACCEPTED_RESOLUTION_STATUSES
    }
    attempts = Counter(str(row.get("navigation_key") or nav_key(
        str(row.get("company_id") or ""), str(row.get("source_url") or row.get("url") or "")
    )) for row in prior_resolutions)

    pending = [
        row for row in candidates
        if row["navigation_key"] not in accepted
        and attempts[row["navigation_key"]] < max(1, args.max_attempts)
    ]
    pending.sort(key=lambda row: (
        normalized_depth(row),
        0 if url_has_navigation_signal(str(row.get("source_url") or "")) else 1,
        str(row.get("company_id") or ""),
        str(row.get("source_url") or ""),
    ))
    pending = pending[: max(1, args.batch_size)]

    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(
                lambda row: audit_navigation(
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

    append_jsonl(resolution_file, resolutions)
    append_jsonl(output_dir / "job_link_candidates_from_navigation.jsonl", details)
    append_jsonl(output_dir / "recruitment_navigation_expanded.jsonl", children)
    append_jsonl(output_dir / "failures_navigation.jsonl", failures)

    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    status_counts = Counter(str(row.get("resolution_status") or "unknown") for row in resolutions)
    state.update({
        "updated_at": utc_now(),
        "navigation_expansion_last_batch": {
            "enumerator_version": "2.0-bounded",
            "candidate_count": len(candidates),
            "legacy_candidates_reclassified": len(legacy_navigation),
            "selected_count": len(pending),
            "processed_count": len(resolutions),
            "detail_candidates_discovered": len(details),
            "child_navigation_discovered": len(children),
            "filtered_non_recruitment_links": sum(int(row.get("filtered_non_recruitment_links") or 0) for row in resolutions),
            "resolution_status_counts": dict(status_counts),
            "max_workers": worker_count,
            "max_children_per_page": max(1, args.max_children_per_page),
        },
    })
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(state.get("navigation_expansion_last_batch", {}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
