from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SEED_PATH = ROOT / "company_seed_1000.json"
DELTA_PATH = ROOT / "seed_delta_batch_0007.json"
OUTPUT_DIR = ROOT / "output"
STATE_PATH = ROOT / "runtime" / "checkpoint.json"
LOG_DIR = ROOT / "logs"
RUN_DIR = ROOT / "runs" / "batch_0007"
CRAWLER = ROOT / "crawl_company_jobs.py"

BATCH_ID = "batch_0007"
START_INDEX = 36
BATCH_SIZE = 10
END_INDEX = START_INDEX + BATCH_SIZE
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return CONTROL_CHARS.sub(" ", value).strip()
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    # Split only on the physical JSONL delimiter. str.splitlines() also splits on
    # U+0085/NEL, which can occur in mis-decoded upstream HTML titles.
    for raw in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(sanitize(row))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(sanitize(row), ensure_ascii=False, sort_keys=True) for row in rows)
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def merge_seed_delta() -> tuple[int, set[str]]:
    seed = read_json(SEED_PATH)
    delta = read_json(DELTA_PATH)
    companies = seed.get("companies", [])
    by_id = {c["company_id"]: c for c in companies if c.get("company_id")}
    order = [c["company_id"] for c in companies if c.get("company_id")]
    batch_ids: set[str] = set()
    for company in delta.get("companies", []):
        company_id = company["company_id"]
        batch_ids.add(company_id)
        if company_id not in by_id:
            order.append(company_id)
        by_id[company_id] = company
    merged = [by_id[company_id] for company_id in order]
    if len(merged) < END_INDEX or len(batch_ids) != BATCH_SIZE:
        raise RuntimeError(f"invalid batch seed: total={len(merged)}, batch_ids={len(batch_ids)}")
    seed["companies"] = merged
    seed["valid_seed_count"] = len(merged)
    seed["target_count"] = 1000
    seed["seed_status"] = "partial_valid_seed_expanding"
    seed["generated_at"] = utc_now()
    SEED_PATH.write_text(json.dumps(seed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(merged), batch_ids


def latest_by_company(rows: list[dict[str, Any]], batch_ids: set[str]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        company_id = str(row.get("company_id") or "")
        if company_id in batch_ids:
            latest[company_id] = row
    return [latest[company_id] for company_id in sorted(latest)]


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    seed_count, batch_ids = merge_seed_delta()
    subprocess.run([sys.executable, "-m", "py_compile", str(CRAWLER)], check=True)

    coverage_path = OUTPUT_DIR / "coverage_auto.jsonl"
    failure_path = OUTPUT_DIR / "failures_auto.jsonl"
    link_path = OUTPUT_DIR / "job_link_candidates_auto.jsonl"

    preexisting_coverage = latest_by_company(load_jsonl(coverage_path), batch_ids)
    preexisting_ids = {str(row.get("company_id") or "") for row in preexisting_coverage}
    crawler_skipped = preexisting_ids == batch_ids
    crawler_return_code = 0
    crawler_stdout = ""
    crawler_stderr = ""

    if crawler_skipped:
        crawler_stdout = json.dumps({
            "skipped": True,
            "reason": "all batch company coverage rows already present",
            "companies": len(preexisting_ids),
        }, ensure_ascii=False)
    else:
        command = [
            sys.executable,
            str(CRAWLER),
            "--seed-file", str(SEED_PATH),
            "--output-dir", str(OUTPUT_DIR),
            "--state-file", str(STATE_PATH),
            "--start-index", str(START_INDEX),
            "--batch-size", str(BATCH_SIZE),
        ]
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        crawler_return_code = completed.returncode
        crawler_stdout = completed.stdout
        crawler_stderr = completed.stderr

    (LOG_DIR / "batch_0007_auto.log").write_text(
        crawler_stdout + ("\n[stderr]\n" + crawler_stderr if crawler_stderr else ""),
        encoding="utf-8",
    )

    coverage_rows = latest_by_company(load_jsonl(coverage_path), batch_ids)
    failure_rows = latest_by_company(load_jsonl(failure_path), batch_ids)
    link_rows = [row for row in load_jsonl(link_path) if str(row.get("company_id") or "") in batch_ids]
    dedup_links: dict[str, dict[str, Any]] = {}
    for row in link_rows:
        key = f"{row.get('company_id', '')}|{row.get('source_url', '')}"
        dedup_links[key] = row
    link_rows = list(dedup_links.values())

    snapshots = {
        "coverage": RUN_DIR / "coverage_batch_0007.jsonl",
        "failures": RUN_DIR / "failures_batch_0007.jsonl",
        "links": RUN_DIR / "job_link_candidates_batch_0007.jsonl",
    }
    write_jsonl(snapshots["coverage"], coverage_rows)
    write_jsonl(snapshots["failures"], failure_rows)
    write_jsonl(snapshots["links"], link_rows)

    status_counts: dict[str, int] = {}
    for row in coverage_rows:
        status = str(row.get("enumeration_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    manifest = {
        "batch_id": BATCH_ID,
        "generated_at_utc": utc_now(),
        "seed_count_after_merge": seed_count,
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "companies_reviewed": len(coverage_rows),
        "status_counts": status_counts,
        "batch_output_counts": {
            snapshots["coverage"].name: len(coverage_rows),
            snapshots["failures"].name: len(failure_rows),
            snapshots["links"].name: len(link_rows),
        },
        "crawler_skipped_existing_complete_batch": crawler_skipped,
        "crawler_return_code": crawler_return_code,
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
        },
        "final_acceptance_met": False,
    }
    write_json(RUN_DIR / "manifest.json", manifest)

    state = read_json(STATE_PATH)
    state["valid_seed_count"] = seed_count
    state["next_batch_start_index"] = END_INDEX
    state.setdefault("batch_progress", {})[BATCH_ID] = {
        "companies_reviewed": len(coverage_rows),
        "records": 0,
        "status": "partial" if crawler_return_code == 0 else "runner_failed",
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "enumeration_status_counts": status_counts,
        "job_link_candidates_discovered": len(link_rows),
        "final_acceptance_met": 0,
        "run_path": "runs/batch_0007",
    }
    state["updated_at"] = utc_now()
    state["last_auto_batch"] = {"start_index": START_INDEX, "count": BATCH_SIZE}
    write_json(STATE_PATH, state)

    print(json.dumps(manifest, ensure_ascii=False))
    if crawler_return_code != 0:
        raise RuntimeError(f"crawler failed with return code {crawler_return_code}")
    if len(coverage_rows) != BATCH_SIZE:
        raise RuntimeError(f"expected {BATCH_SIZE} audited companies, got {len(coverage_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
