#!/usr/bin/env python3
"""Materialize a conservative manual snapshot from Guangdong Nanyue Bank official 2026 announcements.

The source pages expose role names, departments, headcount and generic eligibility rules.
Role-specific duties/requirements remain behind official QR codes or attachments, so records
stay candidate_raw. Expired announcements are explicitly inactive_expired. The script is
idempotent and never sets active_verified.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OBSERVED_AT = "2026-07-15T06:29:53+00:00"
COMPANY_ID = "cn-bank-0344"
COMPANY_NAME = "广东南粤银行股份有限公司"
GENERIC_REQUIREMENTS = (
    "思想政治素质好、品行优良；遵守国家法律法规和金融规章制度，无不良记录；"
    "具备团队协作、适岗、业务创新和风险防控能力；符合监管任职资格要求；"
    "身体及心理健康。"
)


def stable_hash(value: str, length: int) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def make_job(
    page: str,
    published_date: str,
    department: str,
    title: str,
    *,
    city: str | None = None,
    headcount: int | None = None,
    deadline_date: str | None = None,
    review_status: str = "candidate_raw",
    freshness_note: str = "continuous recruitment; application closes when filled",
) -> dict:
    page_url = f"https://www.gdnybank.com/jobnews/{page}.html"
    key = f"{COMPANY_ID}|{page}|{department}|{title}|{city or ''}"
    fragment = stable_hash(f"{department}|{title}|{city or ''}", 10)
    return {
        "active_verified": False,
        "city": city,
        "company": COMPANY_NAME,
        "company_id": COMPANY_ID,
        "company_name": COMPANY_NAME,
        "deadline_date": deadline_date,
        "department": department,
        "detail_status": "official_announcement_role_list_role_specific_duties_or_requirements_attachment_pending",
        "education": None,
        "employment_type": None,
        "experience": None,
        "freshness_note": freshness_note,
        "headcount": headcount,
        "job_name": title,
        "job_title": title,
        "location": city,
        "observed_at": OBSERVED_AT,
        "published_date": published_date,
        "record_id": stable_hash(key, 24),
        "recruitment_category": "social_recruitment",
        "requirements": GENERIC_REQUIREMENTS,
        "responsibilities": None,
        "review_recommendation": review_status,
        "review_status": review_status,
        "salary": None,
        "salary_disclosure_status": "not_disclosed",
        "source_page_url": page_url,
        "source_type": "official_recruitment_announcement",
        "source_url": f"{page_url}#role-{fragment}",
        "verified_at": OBSERVED_AT,
    }


def build_jobs() -> list[dict]:
    jobs: list[dict] = []

    for department, title, headcount in [
        ("总行授信管理部/放款中心（二级部）", "放款审查岗", None),
        ("总行授信管理部/信贷资产分类室", "信贷资产分类管理岗", 1),
        ("总行授信管理部/信贷资产分类室", "信贷资产减值管理岗", 1),
        ("总行授信管理部/授信后管理室", "检查管理岗", 1),
        ("总行授信管理部/授信业务问责室", "授信业务问责岗", 1),
    ]:
        jobs.append(make_job("20260311/38107", "2026-03-11", department, title, headcount=headcount))

    jobs.append(make_job(
        "20260311/38106", "2026-03-11", "总行信息科技部/运维管理室", "动力环境管理岗",
        headcount=1, deadline_date="2026-03-31", review_status="inactive_expired",
        freshness_note="application deadline passed on 2026-03-31",
    ))
    jobs.append(make_job(
        "20260429/39657", "2026-04-29", "总行信息科技部/运维管理室", "动力环境管理岗",
        headcount=1, deadline_date="2026-05-31", review_status="inactive_expired",
        freshness_note="application deadline passed on 2026-05-31",
    ))

    for title in [
        "支行行长",
        "支行副行长/行长助理（分管零售业务）",
        "公司业务团队总经理/副总经理/总经理助理",
        "普惠业务团队总经理/副总经理/总经理助理",
        "零售业务团队总经理/副总经理/总经理助理",
        "对公客户经理",
        "零售主管",
        "理财经理",
        "普惠客户经理",
        "零售客户经理",
        "服务经理",
    ]:
        jobs.append(make_job("20260309/38092", "2026-03-09", "各分行/支行", title))

    for city in ("深圳", "重庆", "长沙"):
        jobs.append(make_job(
            "20260309/38092", "2026-03-09", f"{city}分行/公司金融部", "金融市场岗",
            city=city, headcount=1,
        ))
    for city in ("东莞", "江门"):
        jobs.append(make_job(
            "20260309/38092", "2026-03-09", f"{city}分行/综合部（安全保卫部）", "信息科技岗",
            city=city, headcount=1,
        ))

    for department, title, headcount in [
        ("总行风险管理部/统计分析室", "统计分析岗", 1),
        ("总行风险管理部/模型研发与校验室", "风险模型岗", 1),
        ("总行风险管理部/征信与信贷系统管理室", "信贷系统管理岗", 1),
        ("总行特殊资产经营管理部/业务经营室", "资产清收岗", None),
        ("总行授信审批部/公司金融业务审查室", "公司金融业务审查岗", None),
        ("总行交易银行部/产业金融室（拟设）", "营销推动岗", 1),
        ("总行交易银行部/产业金融室（拟设）", "产品管理岗", 1),
        ("总行交易银行部/结算产品室（拟设）", "系统及产品管理岗", None),
        ("总行交易银行部/国际业务室", "营销推动岗（拟设）", 1),
    ]:
        jobs.append(make_job("20260203/37839", "2026-02-03", department, title, headcount=headcount))

    for department, title, headcount in [
        ("总行零售金融部", "副总经理/总经理助理", 1),
        ("总行财务会计部", "副总经理/总经理助理（资产负债管理方向）", 1),
        ("分行", "分行行长", None),
        ("分行", "副行长/行长助理（分管公司/零售）", None),
        ("分行", "副行长/行长助理（分管风险）", None),
        ("总行信息科技部/运维管理室", "运行管理岗", 1),
        ("资金运营中心/债券部（二级部）", "债券交易岗", None),
    ]:
        jobs.append(make_job("20260120/37629", "2026-01-20", department, title, headcount=headcount))

    return jobs


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    jobs = build_jobs()
    if len(jobs) != 39:
        raise RuntimeError(f"expected 39 jobs, got {len(jobs)}")

    write_jsonl(OUTPUT / "jobs_manual_gdny_20260715_pass2.jsonl", jobs)

    coverage = [{
        "audit_key": stable_hash(f"{COMPANY_ID}|20260715|pass2", 20),
        "company_id": COMPANY_ID,
        "company_name": COMPANY_NAME,
        "enumeration_status": "official_2026_index_fully_accounted_detail_normalization_partial",
        "error": None,
        "final_acceptance_met": False,
        "final_url": "https://www.gdnybank.com/jobnews/",
        "http_status": 200,
        "job_link_candidate_count": 11,
        "normalized_announcement_pages_total": 9,
        "normalized_roles_total": 55,
        "observed_at": OBSERVED_AT,
        "official_entry_url": "https://www.gdnybank.com/jobnews/",
        "official_entry_verified": True,
        "page_title": "广东南粤银行_社会招聘",
        "pending_detail_pages": 2,
        "pending_detail_urls": [
            "https://www.gdnybank.com/jobnews/20260630/40722.html",
            "https://www.gdnybank.com/jobnews/20260213/38009.html",
        ],
        "roles_materialized_this_pass": 39,
    }]
    write_jsonl(OUTPUT / "coverage_manual_gdny_20260715_pass2.jsonl", coverage)

    failures = []
    for page in (
        "20260429/39657", "20260311/38107", "20260311/38106",
        "20260309/38092", "20260203/37839", "20260120/37629",
    ):
        failures.append({
            "access_control_bypassed": False,
            "company_id": COMPANY_ID,
            "company_name": COMPANY_NAME,
            "http_status": 200,
            "notes": "HTML exposed role names and generic conditions; role-specific duties/requirements remain in an official QR code or attachment and were not inferred.",
            "observed_at": OBSERVED_AT,
            "reason": "role_specific_duties_and_requirements_only_in_attachment_or_qr_pending_manual_enumeration",
            "retryable": True,
            "url": f"https://www.gdnybank.com/jobnews/{page}.html",
        })
    for page in ("20260630/40722", "20260213/38009"):
        failures.append({
            "access_control_bypassed": False,
            "company_id": COMPANY_ID,
            "company_name": COMPANY_NAME,
            "http_status": None,
            "notes": "Official index confirmed the announcement; public detail fetch returned a transient cache miss and content was not inferred.",
            "observed_at": OBSERVED_AT,
            "reason": "transient_public_detail_fetch_cache_miss",
            "retryable": True,
            "url": f"https://www.gdnybank.com/jobnews/{page}.html",
        })
    write_jsonl(OUTPUT / "failures_manual_gdny_20260715_pass2.jsonl", failures)

    print(json.dumps({
        "jobs": len(jobs),
        "candidate_raw": sum(row["review_status"] == "candidate_raw" for row in jobs),
        "inactive_expired": sum(row["review_status"] == "inactive_expired" for row in jobs),
        "active_verified": 0,
        "coverage_records": len(coverage),
        "failure_records": len(failures),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
