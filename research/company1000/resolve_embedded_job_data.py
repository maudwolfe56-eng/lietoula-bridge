#!/usr/bin/env python3
"""Resolve official job details embedded in JSON or client-side page state.

Many official career portals expose the job payload in JSON-LD, ``__NEXT_DATA__`` or an
initial-state script even when the visible HTML is a JavaScript shell. This resolver only
reads publicly returned bytes. It does not execute a browser, log in, solve CAPTCHA, call
private endpoints, infer salary, or promote records to ``active_verified``.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, urldefrag

import requests
from bs4 import BeautifulSoup

from crawl_company_jobs import USER_AGENT, utc_now

ROOT = Path(__file__).resolve().parent
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm", "from", "source", "ref", "referer", "tracking", "track"}
TITLE_KEYS = {"jobtitle", "job_title", "positionname", "position_name", "jobname", "job_name", "postname", "post_name", "requisitiontitle", "title"}
RESP_KEYS = {"responsibilities", "responsibility", "jobresponsibilities", "job_responsibilities", "duties", "duty", "jobdescription", "job_description", "jobdesc", "description", "workcontent", "work_content", "content"}
REQ_KEYS = {"requirements", "requirement", "jobrequirements", "job_requirements", "qualifications", "qualification", "jobqualification", "job_qualification", "conditions", "candidateprofile", "candidate_profile"}
LOCATION_KEYS = {"location", "joblocation", "job_location", "worklocation", "work_location", "city", "address", "place", "locations"}
DEPARTMENT_KEYS = {"department", "departmentname", "department_name", "businessunit", "business_unit", "function", "jobfamily", "job_family"}
EXPERIENCE_KEYS = {"experience", "workexperience", "work_experience", "experienceyears", "experience_years"}
EDUCATION_KEYS = {"education", "educationlevel", "education_level", "degree", "qualificationlevel", "qualification_level"}
PUBLISH_KEYS = {"dateposted", "publishdate", "publish_date", "posteddate", "posted_date", "releasedate", "release_date", "createdate", "create_date"}
DEADLINE_KEYS = {"validthrough", "deadline", "expirydate", "expiry_date", "enddate", "end_date", "closedate", "close_date"}
SALARY_KEYS = {"basesalary", "salary", "salaryrange", "salary_range", "compensation", "payrange", "pay_range"}
CLOSED_WORDS = re.compile(r"职位已关闭|招聘已结束|已下线|已过期|position closed|job closed|no longer available|expired", re.I)
HTML_TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")


def canonical_url(url: str) -> str:
    url = urldefrag(url)[0].strip()
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, urlencode(query, doseq=True), ""))


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


def load_glob(directory: Path, pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        rows.extend(read_jsonl(path))
    return rows


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def clean_text(value: Any, limit: int = 20000) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        if isinstance(value, dict):
            pieces = [clean_text(v, limit=limit) for v in value.values()]
        else:
            pieces = [clean_text(v, limit=limit) for v in value]
        text = "；".join(piece for piece in pieces if piece)
    else:
        text = str(value)
    text = html.unescape(text)
    text = HTML_TAG.sub(" ", text)
    text = text.replace("\\n", "\n").replace("\\r", " ").replace("\\t", " ")
    text = SPACE.sub(" ", text).strip(" \t\r\n,;；|")
    if not text or text.lower() in {"null", "none", "undefined", "{}", "[]"}:
        return None
    return text[:limit]


def lower_map(obj: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    return {str(key).lower().replace("-", "_"): (str(key), value) for key, value in obj.items()}


def first_value(mapping: dict[str, tuple[str, Any]], keys: set[str]) -> Any:
    for key in keys:
        normalized = key.lower().replace("-", "_")
        if normalized in mapping:
            return mapping[normalized][1]
    return None


def split_description(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    resp_match = re.search(r"(?:岗位职责|职位职责|工作职责|Responsibilities?)\s*[:：]?", text, re.I)
    req_match = re.search(r"(?:任职要求|任职资格|职位要求|岗位要求|Qualifications?|Requirements?)\s*[:：]?", text, re.I)
    if resp_match and req_match:
        if resp_match.start() < req_match.start():
            responsibilities = text[resp_match.end():req_match.start()].strip()
            requirements = text[req_match.end():].strip()
        else:
            requirements = text[req_match.end():resp_match.start()].strip()
            responsibilities = text[resp_match.end():].strip()
        return clean_text(responsibilities), clean_text(requirements)
    return clean_text(text), None


def job_candidate_from_dict(obj: dict[str, Any], path: str) -> dict[str, Any] | None:
    mapping = lower_map(obj)
    type_value = clean_text(first_value(mapping, {"@type", "type"}), limit=200)
    strong_type = bool(type_value and "jobposting" in type_value.lower())
    title_raw = first_value(mapping, TITLE_KEYS)
    title = clean_text(title_raw, limit=500)
    if not title:
        return None
    generic_name_only = "name" in mapping and not any(key in mapping for key in TITLE_KEYS - {"title"})
    context_job_signal = bool(re.search(r"job|position|posting|requisition|vacancy|career", path, re.I))

    description = clean_text(first_value(mapping, RESP_KEYS))
    responsibilities, description_requirements = split_description(description)
    requirements = clean_text(first_value(mapping, REQ_KEYS)) or description_requirements
    location = clean_text(first_value(mapping, LOCATION_KEYS), limit=1000)
    department = clean_text(first_value(mapping, DEPARTMENT_KEYS), limit=1000)
    experience = clean_text(first_value(mapping, EXPERIENCE_KEYS), limit=1000)
    education = clean_text(first_value(mapping, EDUCATION_KEYS), limit=1000)
    publish_date = clean_text(first_value(mapping, PUBLISH_KEYS), limit=200)
    deadline = clean_text(first_value(mapping, DEADLINE_KEYS), limit=200)
    salary = clean_text(first_value(mapping, SALARY_KEYS), limit=1000)

    evidence_count = sum(bool(value) for value in [responsibilities, requirements, location, department, experience, education, publish_date, deadline, salary])
    if not strong_type and generic_name_only and not context_job_signal:
        return None
    if not strong_type and evidence_count < 2:
        return None
    if len(title) > 300 or title.lower() in {"careers", "jobs", "job details", "职位详情", "加入我们"}:
        return None
    return {
        "title": title,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "location": location,
        "department": department,
        "experience": experience,
        "education": education,
        "publish_date": publish_date,
        "deadline": deadline,
        "salary": salary,
        "strong_type": strong_type,
        "evidence_count": evidence_count,
        "json_path": path,
    }


def walk_json(value: Any, path: str = "$", depth: int = 0) -> list[dict[str, Any]]:
    if depth > 12:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        candidate = job_candidate_from_dict(value, path)
        if candidate:
            found.append(candidate)
        for key, child in value.items():
            found.extend(walk_json(child, f"{path}.{key}", depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value[:1000]):
            found.extend(walk_json(child, f"{path}[{index}]", depth + 1))
    return found


def balanced_json(text: str, start: int) -> str | None:
    if start >= len(text) or text[start] not in "[{":
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index in range(start, min(len(text), start + 5_000_000)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def parse_json_text(text: str) -> Any | None:
    text = text.strip().rstrip(";")
    if not text:
        return None
    for candidate in [text, html.unescape(text)]:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def embedded_payloads(response: requests.Response) -> list[tuple[str, Any]]:
    payloads: list[tuple[str, Any]] = []
    body = response.text or ""
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type or body.lstrip().startswith(("{", "[")):
        parsed = parse_json_text(body)
        if parsed is not None:
            payloads.append(("response_json", parsed))
    soup = BeautifulSoup(body, "lxml")
    for index, script in enumerate(soup.find_all("script")):
        text = script.string or script.get_text() or ""
        script_type = str(script.get("type") or "").lower()
        script_id = str(script.get("id") or "")
        if "json" in script_type or script_id in {"__NEXT_DATA__", "__NUXT_DATA__"}:
            parsed = parse_json_text(text)
            if parsed is not None:
                payloads.append((f"script:{script_id or script_type}:{index}", parsed))
    assignment_markers = ["__NEXT_DATA__", "__INITIAL_STATE__", "__NUXT__", "INITIAL_STATE", "jobData", "jobDetail", "positionData", "positionInfo", "phApp.ddo"]
    for marker in assignment_markers:
        for match in re.finditer(re.escape(marker), body):
            tail = body[match.end():match.end() + 500]
            relative = min([pos for pos in [tail.find("{"), tail.find("[")] if pos >= 0], default=-1)
            if relative < 0:
                continue
            start = match.end() + relative
            blob = balanced_json(body, start)
            if blob:
                parsed = parse_json_text(blob)
                if parsed is not None:
                    payloads.append((f"assignment:{marker}", parsed))
                    break
    return payloads


def regex_fallback(body: str) -> dict[str, Any] | None:
    aliases = {
        "title": ["jobTitle", "positionName", "jobName", "requisitionTitle"],
        "responsibilities": ["jobDescription", "description", "responsibilities", "duties"],
        "requirements": ["jobRequirements", "requirements", "qualifications"],
        "location": ["jobLocation", "workLocation", "locationName", "location"],
        "department": ["departmentName", "businessUnit", "department"],
        "publish_date": ["datePosted", "publishDate", "postedDate"],
        "deadline": ["validThrough", "deadline", "expiryDate"],
    }
    result: dict[str, Any] = {}
    for field, keys in aliases.items():
        for key in keys:
            pattern = re.compile(rf'["\']{re.escape(key)}["\']\s*:\s*["\']((?:\\.|[^"\']){{1,30000}}?)["\']\s*[,}}]', re.I | re.S)
            match = pattern.search(body)
            if match:
                raw = match.group(1)
                try:
                    raw = bytes(raw, "utf-8").decode("unicode_escape")
                except UnicodeDecodeError:
                    pass
                result[field] = clean_text(raw)
                break
    if not result.get("title") or sum(bool(result.get(key)) for key in ["responsibilities", "requirements", "location"]) < 1:
        return None
    result.update({"strong_type": False, "evidence_count": sum(bool(v) for v in result.values()), "json_path": "regex_fallback"})
    return result


def choose_candidate(candidates: list[dict[str, Any]], source_url: str) -> dict[str, Any] | None:
    if not candidates:
        return None
    slug = urlparse(source_url).path.lower().replace("-", " ").replace("_", " ")
    def score(candidate: dict[str, Any]) -> tuple[int, int, int]:
        title = str(candidate.get("title") or "").lower()
        title_words = [word for word in re.split(r"\W+", title) if len(word) >= 4]
        slug_match = sum(1 for word in title_words if word in slug)
        completeness = sum(bool(candidate.get(field)) for field in ["title", "responsibilities", "requirements", "location", "department", "experience", "education", "publish_date", "deadline", "salary"])
        content_length = sum(len(str(candidate.get(field) or "")) for field in ["responsibilities", "requirements"])
        return (10 if candidate.get("strong_type") else 0) + slug_match, completeness, content_length
    return max(candidates, key=score)


def completeness(candidate: dict[str, Any]) -> int:
    fields = ["title", "responsibilities", "requirements", "location", "department", "experience", "education", "publish_date", "deadline"]
    return round(100 * sum(bool(candidate.get(field)) for field in fields) / len(fields))


def build_job(row: dict[str, Any], candidate: dict[str, Any], final_url: str, observed_at: str, closed: bool, extraction_source: str) -> dict[str, Any]:
    title = clean_text(candidate.get("title"), limit=500) or ""
    location = clean_text(candidate.get("location"), limit=1000)
    responsibilities = clean_text(candidate.get("responsibilities"))
    requirements = clean_text(candidate.get("requirements"))
    salary = clean_text(candidate.get("salary"), limit=1000)
    score = completeness(candidate)
    review_status = "inactive_expired" if closed else ("review_pending" if title and location and responsibilities and requirements else "candidate_raw")
    recommendation = "archive_closed_or_expired" if closed else ("manual_review_before_publish" if review_status == "review_pending" else "supplement_missing_required_fields")
    source_url = canonical_url(str(row.get("source_url") or final_url))
    job_id = hashlib.sha256(f"{row.get('company_id')}|{source_url}|{title}".encode()).hexdigest()[:24]
    return {
        "job_id": job_id,
        "company_id": row.get("company_id"),
        "company_name": row.get("company_name"),
        "job_title": title,
        "title": title,
        "department": clean_text(candidate.get("department"), limit=1000),
        "responsibilities": responsibilities,
        "duties": responsibilities,
        "requirements": requirements,
        "city": location,
        "location": location,
        "salary": salary,
        "salary_disclosure": "disclosed" if salary else "not_disclosed",
        "salary_disclosure_status": "disclosed" if salary else "not_disclosed",
        "experience": clean_text(candidate.get("experience"), limit=1000),
        "education": clean_text(candidate.get("education"), limit=1000),
        "publish_date": clean_text(candidate.get("publish_date"), limit=200),
        "published_at": clean_text(candidate.get("publish_date"), limit=200),
        "deadline": clean_text(candidate.get("deadline"), limit=200),
        "source_url": source_url,
        "final_url": final_url,
        "source_type": "official_job_detail_embedded_data",
        "verified_at": observed_at,
        "observed_at": observed_at,
        "completeness": score,
        "completeness_score": score,
        "confidence": "high" if candidate.get("strong_type") else "medium",
        "confidence_score": 0.9 if candidate.get("strong_type") else 0.75,
        "review_status": review_status,
        "review_recommendation": recommendation,
        "promotion_eligible": False,
        "active_verified": False,
        "extraction_source": extraction_source,
        "extraction_json_path": candidate.get("json_path"),
    }


def resolve_one(row: dict[str, Any], timeout: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_url = canonical_url(str(row.get("source_url") or ""))
    observed_at = utc_now()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})
    try:
        response = session.get(source_url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return None, {"company_id": row.get("company_id"), "company_name": row.get("company_name"), "source_url": source_url, "url": source_url, "reason": "embedded_resolver_fetch_failed", "detail": type(exc).__name__, "observed_at": observed_at}
    if response.status_code in {401, 403, 429}:
        return None, {"company_id": row.get("company_id"), "company_name": row.get("company_name"), "source_url": source_url, "url": source_url, "reason": "detail_access_restricted", "detail": f"http_{response.status_code}", "http_status": response.status_code, "observed_at": observed_at}
    if response.status_code in {404, 410}:
        return None, {"company_id": row.get("company_id"), "company_name": row.get("company_name"), "source_url": source_url, "url": source_url, "reason": "detail_closed_or_removed", "detail": f"http_{response.status_code}", "http_status": response.status_code, "observed_at": observed_at}
    if response.status_code >= 500:
        return None, {"company_id": row.get("company_id"), "company_name": row.get("company_name"), "source_url": source_url, "url": source_url, "reason": "embedded_resolver_http_retry", "detail": f"http_{response.status_code}", "http_status": response.status_code, "observed_at": observed_at}

    candidates: list[dict[str, Any]] = []
    payload_sources: list[str] = []
    for payload_source, payload in embedded_payloads(response):
        found = walk_json(payload, path=payload_source)
        if found:
            payload_sources.append(payload_source)
            candidates.extend(found)
    if not candidates:
        fallback = regex_fallback(response.text or "")
        if fallback:
            candidates.append(fallback)
            payload_sources.append("regex_fallback")
    chosen = choose_candidate(candidates, source_url)
    if not chosen:
        return None, {"company_id": row.get("company_id"), "company_name": row.get("company_name"), "source_url": source_url, "url": source_url, "reason": "embedded_job_data_not_found", "detail": "public_response_contains_no_conservative_job_payload", "http_status": response.status_code, "observed_at": observed_at}
    closed = bool(CLOSED_WORDS.search(BeautifulSoup(response.text or "", "lxml").get_text(" ", strip=True)[:50000]))
    job = build_job(row, chosen, response.url, observed_at, closed, ",".join(payload_sources[:10]))
    return job, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--batch-size", type=int, default=240)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args()
    output = ROOT / args.output_dir

    classifications = read_jsonl(output / "detail_candidate_classification.jsonl")
    suppressed = {canonical_url(str(row.get("source_url") or "")) for row in classifications if row.get("terminal_for_detail_resolution") and row.get("source_url")}
    jobs = load_glob(output, "*jobs*.jsonl")
    resolved = {canonical_url(str(row.get("source_url") or "")) for row in jobs if row.get("source_url")}
    failure_rows = load_glob(output, "*failures*.jsonl")
    retryable_urls = {
        canonical_url(str(row.get("source_url") or row.get("url") or ""))
        for row in failure_rows
        if str(row.get("reason") or "") in {"detail_page_unparseable", "detail_page_empty", "detail_unparseable_or_multi_position_announcement", "embedded_job_data_not_found"}
    }

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in load_glob(output, "*job_link_candidates*.jsonl"):
        company_id = str(row.get("company_id") or "").strip()
        url = canonical_url(str(row.get("source_url") or ""))
        if not company_id or not url or url in suppressed or url in resolved:
            continue
        host = urlparse(url).netloc.lower()
        dynamic_hint = host in {"jobs.lenovo.com", "www.fenbi.com"} or "job" in host or "career" in host
        if url in retryable_urls or dynamic_hint:
            normalized = dict(row)
            normalized["source_url"] = url
            unique[(company_id, url)] = normalized
    pending = sorted(unique.values(), key=lambda row: (0 if urlparse(str(row.get("source_url") or "")).netloc.lower() == "jobs.lenovo.com" else 1, str(row.get("company_id") or ""), str(row.get("source_url") or "")))[: max(1, args.batch_size)]

    worker_count = max(1, min(args.max_workers, len(pending) or 1))
    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(lambda row: resolve_one(row, max(5, args.timeout_seconds)), pending))
    job_rows = [job for job, _ in results if job]
    failure_new = [failure for _, failure in results if failure]
    append_jsonl(output / "jobs_resolved_embedded_auto.jsonl", job_rows)
    append_jsonl(output / "failures_embedded_job_data_auto.jsonl", failure_new)

    state = {
        "updated_at": utc_now(),
        "resolver_version": "1.0-public-embedded-json",
        "selected": len(pending),
        "jobs_created": len(job_rows),
        "failures_created": len(failure_new),
        "failure_reason_counts": dict(Counter(str(row.get("reason") or "unknown") for row in failure_new)),
        "review_status_counts": dict(Counter(str(row.get("review_status") or "unknown") for row in job_rows)),
        "policy": {"public_bytes_only": True, "auto_active_verified": False, "salary_inference_allowed": False, "access_control_bypass_allowed": False},
    }
    (ROOT / "runtime").mkdir(parents=True, exist_ok=True)
    (ROOT / "runtime/embedded_detail_resolution_checkpoint.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
