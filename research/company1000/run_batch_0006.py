#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BATCH_ID = "batch_0006"
START_INDEX = 26
BATCH_SIZE = 10
END_INDEX = START_INDEX + BATCH_SIZE


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")


def merge_seed() -> int:
    seed_path = ROOT / "company_seed_1000.json"
    delta_path = ROOT / "seed_delta_batch_0006.json"
    seed = read_json(seed_path)
    delta = read_json(delta_path)
    old = seed.get("companies", [])
    by_id = {c["company_id"]: c for c in old if c.get("company_id")}
    order = [c["company_id"] for c in old if c.get("company_id")]
    for company in delta.get("companies", []):
        cid = company["company_id"]
        if cid not in by_id:
            order.append(cid)
        by_id[cid] = company
    merged = [by_id[cid] for cid in order]
    if len(merged) < END_INDEX:
        raise RuntimeError(f"expected at least {END_INDEX} seeds, got {len(merged)}")
    seed.update(
        {
            "companies": merged,
            "valid_seed_count": len(merged),
            "target_count": 1000,
            "seed_status": "partial_valid_seed_expanding",
            "generated_at": now(),
        }
    )
    write_json(seed_path, seed, compact=True)
    return len(merged)


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def materialize_delta(src: Path, before: int, dst: Path) -> int:
    lines = src.read_text(encoding="utf-8", errors="replace").splitlines() if src.exists() else []
    new_lines = lines[before:]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(("\n".join(new_lines) + "\n") if new_lines else "", encoding="utf-8")
    return len(new_lines)


def main() -> int:
    seed_count = merge_seed()
    output = ROOT / "output"
    runtime = ROOT / "runtime"
    logs = ROOT / "logs"
    run_dir = ROOT / "runs" / BATCH_ID
    for p in (output, runtime, logs, run_dir):
        p.mkdir(parents=True, exist_ok=True)

    coverage = output / "coverage_auto.jsonl"
    failures = output / "failures_auto.jsonl"
    links = output / "job_link_candidates_auto.jsonl"
    before = {
        "coverage": line_count(coverage),
        "failures": line_count(failures),
        "links": line_count(links),
    }
    write_json(run_dir / "offsets_before.json", before)

    command = [
        sys.executable,
        str(ROOT / "crawl_company_jobs.py"),
        "--seed-file",
        str(ROOT / "company_seed_1000.json"),
        "--output-dir",
        str(output),
        "--state-file",
        str(runtime / "checkpoint.json"),
        "--start-index",
        str(START_INDEX),
        "--batch-size",
        str(BATCH_SIZE),
    ]
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    (logs / f"{BATCH_ID}_auto.log").write_text(
        proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""),
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"crawler failed with exit {proc.returncode}: {proc.stderr[-1000:]}")

    counts = {
        "coverage_batch_0006.jsonl": materialize_delta(
            coverage, before["coverage"], run_dir / "coverage_batch_0006.jsonl"
        ),
        "failures_batch_0006.jsonl": materialize_delta(
            failures, before["failures"], run_dir / "failures_batch_0006.jsonl"
        ),
        "job_link_candidates_batch_0006.jsonl": materialize_delta(
            links, before["links"], run_dir / "job_link_candidates_batch_0006.jsonl"
        ),
    }

    coverage_rows = []
    coverage_batch = run_dir / "coverage_batch_0006.jsonl"
    for line in coverage_batch.read_text(encoding="utf-8").splitlines():
        if line.strip():
            coverage_rows.append(json.loads(line))
    status_counts: dict[str, int] = {}
    for row in coverage_rows:
        status = row.get("enumeration_status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    manifest = {
        "batch_id": BATCH_ID,
        "generated_at_utc": now(),
        "seed_count_after_merge": seed_count,
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "companies_reviewed": len(coverage_rows),
        "status_counts": status_counts,
        "new_output_counts": counts,
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
        },
        "final_acceptance_met": False,
    }
    write_json(run_dir / "manifest.json", manifest)

    state_path = runtime / "checkpoint.json"
    state = read_json(state_path) if state_path.exists() else {}
    state["valid_seed_count"] = seed_count
    state["next_batch_start_index"] = END_INDEX
    state["updated_at"] = now()
    state.setdefault("batch_progress", {})[BATCH_ID] = {
        "companies_reviewed": len(coverage_rows),
        "records": 0,
        "status": "partial",
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "enumeration_status_counts": status_counts,
        "job_link_candidates_discovered": counts["job_link_candidates_batch_0006.jsonl"],
        "final_acceptance_met": 0,
        "run_path": f"runs/{BATCH_ID}",
    }
    write_json(state_path, state)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
