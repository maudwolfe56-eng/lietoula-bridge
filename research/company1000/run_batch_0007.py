from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_seed_delta() -> int:
    seed = read_json(SEED_PATH)
    delta = read_json(DELTA_PATH)
    companies = seed.get("companies", [])
    by_id = {c["company_id"]: c for c in companies if c.get("company_id")}
    order = [c["company_id"] for c in companies if c.get("company_id")]
    for company in delta.get("companies", []):
        company_id = company["company_id"]
        if company_id not in by_id:
            order.append(company_id)
        by_id[company_id] = company
    merged = [by_id[company_id] for company_id in order]
    if len(merged) < END_INDEX:
        raise RuntimeError(f"expected at least {END_INDEX} seed companies, got {len(merged)}")
    seed["companies"] = merged
    seed["valid_seed_count"] = len(merged)
    seed["target_count"] = 1000
    seed["seed_status"] = "partial_valid_seed_expanding"
    seed["generated_at"] = utc_now()
    SEED_PATH.write_text(json.dumps(seed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(merged)


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def read_new_lines(path: Path, before: int) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()[before:]


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    seed_count = merge_seed_delta()
    subprocess.run([sys.executable, "-m", "py_compile", str(CRAWLER)], check=True)

    tracked = {
        "coverage": OUTPUT_DIR / "coverage_auto.jsonl",
        "failures": OUTPUT_DIR / "failures_auto.jsonl",
        "links": OUTPUT_DIR / "job_link_candidates_auto.jsonl",
    }
    offsets = {name: count_lines(path) for name, path in tracked.items()}
    write_json(RUN_DIR / "offsets_before.json", offsets)

    command = [
        sys.executable,
        str(CRAWLER),
        "--seed-file",
        str(SEED_PATH),
        "--output-dir",
        str(OUTPUT_DIR),
        "--state-file",
        str(STATE_PATH),
        "--start-index",
        str(START_INDEX),
        "--batch-size",
        str(BATCH_SIZE),
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    (LOG_DIR / "batch_0007_auto.log").write_text(
        completed.stdout + ("\n[stderr]\n" + completed.stderr if completed.stderr else ""),
        encoding="utf-8",
    )

    snapshots = {
        "coverage": RUN_DIR / "coverage_batch_0007.jsonl",
        "failures": RUN_DIR / "failures_batch_0007.jsonl",
        "links": RUN_DIR / "job_link_candidates_batch_0007.jsonl",
    }
    new_counts: dict[str, int] = {}
    for name, source in tracked.items():
        lines = read_new_lines(source, offsets[name])
        snapshots[name].write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        new_counts[snapshots[name].name] = len(lines)

    coverage_rows = []
    for line in snapshots["coverage"].read_text(encoding="utf-8").splitlines():
        if line.strip():
            coverage_rows.append(json.loads(line))
    status_counts: dict[str, int] = {}
    for row in coverage_rows:
        status = row.get("enumeration_status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    manifest = {
        "batch_id": BATCH_ID,
        "generated_at_utc": utc_now(),
        "seed_count_after_merge": seed_count,
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "companies_reviewed": len(coverage_rows),
        "status_counts": status_counts,
        "new_output_counts": new_counts,
        "crawler_return_code": completed.returncode,
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
        "status": "partial" if completed.returncode == 0 else "runner_failed",
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "enumeration_status_counts": status_counts,
        "job_link_candidates_discovered": new_counts.get("job_link_candidates_batch_0007.jsonl", 0),
        "final_acceptance_met": 0,
        "run_path": "runs/batch_0007",
    }
    state["updated_at"] = utc_now()
    state["last_auto_batch"] = {"start_index": START_INDEX, "count": BATCH_SIZE}
    write_json(STATE_PATH, state)

    print(json.dumps(manifest, ensure_ascii=False))
    if completed.returncode != 0:
        raise RuntimeError(f"crawler failed with return code {completed.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
