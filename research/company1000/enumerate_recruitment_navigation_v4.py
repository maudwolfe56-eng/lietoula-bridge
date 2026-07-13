#!/usr/bin/env python3
"""ATS-aware wrapper for the bounded retry-aware navigation enumerator."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import enumerate_recruitment_navigation_v3 as runner

ORIGINAL = runner.nav.looks_like_role_link
STRONG_DETAIL_PATH = re.compile(
    r"getOnePosition|(?:job|position)(?:Detail|Info|View)|view(?:Job|Position)|"
    r"/jobs?/(?:detail/)?[^/?#]+|/positions?/(?:detail/)?[^/?#]+|"
    r"/requisitions?/[^/?#]+|/vacanc(?:y|ies)/[^/?#]+",
    re.I,
)
LIST_OR_NONJOB_PATH = re.compile(
    r"getPositionList|search|privacy|login|news|policy|alternativePosition",
    re.I,
)
DETAIL_QUERY_KEYS = {
    "postidenc", "postid", "jobid", "job_id", "positionid", "position_id",
    "requisitionid", "requisition_id", "reqid", "vacancyid", "vacancy_id",
    "postingid", "posting_id",
}


def ats_detail_link(url: str, label: str) -> bool:
    parsed = urlparse(url)
    target = f"{parsed.path}?{parsed.query}"
    if LIST_OR_NONJOB_PATH.search(target) and not re.search(r"getOnePosition", target, re.I):
        return False
    query = {name.lower(): values for name, values in parse_qs(parsed.query).items()}
    if any(
        name in DETAIL_QUERY_KEYS and any(str(value).strip() for value in values)
        for name, values in query.items()
    ):
        return True
    if STRONG_DETAIL_PATH.search(target):
        return True
    return ORIGINAL(url, label)


runner.nav.looks_like_role_link = ats_detail_link

if __name__ == "__main__":
    raise SystemExit(runner.main())
