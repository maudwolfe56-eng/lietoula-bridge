#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
BATCH_ID = "batch_0006"
START_INDEX = 26
BATCH_SIZE = 10
END_INDEX = START_INDEX + BATCH_SIZE


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read records using LF only; U+0085 inside mojibake titles is not a record separator."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ASCII escaping makes C1 characters such as U+0085 safe for line-oriented tools.
    text = "".join(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def dedupe(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = key_fn(row)
        if not key:
            continue
        if key not in latest:
            order.append(key)
        latest[key] = row
    return [latest[key] for key in order]


def normalize_outputs(output: Path) -> None:
    specs = [
        ("coverage_auto.jsonl", lambda r: str(r.get("audit_key") or "")),
        (
            "failures_auto.jsonl",
            lambda r: "|".join(
                str(r.get(k) or "") for k in ("company_id", "url", "reason", "http_status")
            ),
        ),
        (
            "job_link_candidates_auto.jsonl",
            lambda r: "|".join(str(r.get(k) or "") for k in ("company_id", "source_url")),
        ),
    ]
    for filename, key_fn in specs:
        path = output / filename
        write_jsonl(path, dedupe(read_jsonl(path), key_fn))


def merge_seed() -> tuple[int, list[dict[str, Any]]]:
    seed_path = ROOT / "company_seed_1000.json"
    delta_path = ROOT / "seed_delta_batch_0006.json"
    seed = read_json(seed_path)
    delta = read_json(delta_path)
    old = seed.get("companies", [])
    by_id = {c["company_id"]: c for c in old if isinstance(c, dict) and c.get("company_id")}
    order = [c["company_id"] for c in old if isinstance(c, dict) and c.get("company_id")]
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
    return len(merged), merged


def main() -> int:
    seed_count, companies = merge_seed()
    output = ROOT / "output"
    runtime = ROOT / "runtime"
    logs = ROOT / "logs"
    run_dir = ROOT / "runs" / BATCH_ID
    for path in (output, runtime, logs, run_dir):
        path.mkdir(parents=True, exist_ok=True)

    normalize_outputs(output)
    coverage_path = output / "coverage_auto.jsonl"
    failure_path = output / "failures_auto.jsonl"
    link_path = output / "job_link_candidates_auto.jsonl"
    before = {
        "coverage": len(read_jsonl(coverage_path)),
        "failures": len(read_jsonl(failure_path)),
        "links": len(read_jsonl(link_path)),
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
        proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""), encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"crawler failed with exit {proc.returncode}: {proc.stderr[-1000:]}")

    normalize_outputs(output)
    batch_companies = companies[START_INDEX:END_INDEX]
    batch_ids = {str(company["company_id"]) for company in batch_companies}

    coverage_rows = [r for r in read_jsonl(coverage_path) if str(r.get("company_id")) in batch_ids]
    coverage_rows = dedupe(coverage_rows, lambda r: str(r.get("audit_key") or ""))
    failure_rows = [r for r in read_jsonl(failure_path) if str(r.get("company_id")) in batch_ids]
    failure_rows = dedupe(
        failure_rows,
        lambda r: "|".join(str(r.get(k) or "") for k in ("company_id", "url", "reason", "http_status")),
    )
    link_rows = [r for r in read_jsonl(link_path) if str(r.get("company_id")) in batch_ids]
    link_rows = dedupe(
        link_rows, lambda r: "|".join(str(r.get(k) or "") for k in ("company_id", "source_url"))
    )

    write_jsonl(run_dir / "coverage_batch_0006.jsonl", coverage_rows)
    write_jsonl(run_dir / "failures_batch_0006.jsonl", failure_rows)
    write_jsonl(run_dir / "job_link_candidates_batch_0006.jsonl", link_rows)

    status_counts: dict[str, int] = {}
    for row in coverage_rows:
        status = str(row.get("enumeration_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    observed_ids = {str(row.get("company_id")) for row in coverage_rows}
    unobserved_ids = sorted(batch_ids - observed_ids)

    manifest = {
        "batch_id": BATCH_ID,
        "generated_at_utc": now(),
        "seed_count_after_merge": seed_count,
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "companies_targeted": len(batch_ids),
        "companies_reviewed": len(observed_ids),
        "unobserved_company_ids": unobserved_ids,
        "status_counts": status_counts,
        "batch_output_counts": {
            "coverage": len(coverage_rows),
            "failures": len(failure_rows),
            "job_link_candidates": len(link_rows),
        },
        "policy": {
            "auto_active_verified": False,
            "salary_inference_allowed": False,
            "login_captcha_or_paywall_bypass_allowed": False,
        },
        "final_acceptance_met": False,
    }
    write_json(run_dir / "manifest.json", manifest)
    failure_json = run_dir / "failure.json"
    if failure_json.exists():
        failure_json.unlink()

    state_path = runtime / "checkpoint.json"
    state = read_json(state_path) if state_path.exists() else {}
    state["valid_seed_count"] = seed_count
    state["next_batch_start_index"] = END_INDEX
    state["updated_at"] = now()
    state.setdefault("batch_progress", {})[BATCH_ID] = {
        "companies_reviewed": len(observed_ids),
        "records": 0,
        "status": "partial",
        "seed_start_index": START_INDEX,
        "seed_end_index_exclusive": END_INDEX,
        "enumeration_status_counts": status_counts,
        "job_link_candidates_discovered": len(link_rows),
        "unobserved_company_ids": unobserved_ids,
        "final_acceptance_met": 0,
        "run_path": f"runs/{BATCH_ID}",
    }
    write_json(state_path, state)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def run_with_diagnostics() -> int:
    try:
        return main()
    except Exception as exc:
        run_dir = ROOT / "runs" / BATCH_ID
        logs = ROOT / "logs"
        run_dir.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        details = {
            "batch_id": BATCH_ID,
            "generated_at_utc": now(),
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
            "final_acceptance_met": False,
        }
        write_json(run_dir / "failure.json", details)
        (logs / f"{BATCH_ID}_runner_failure.log").write_text(details["traceback"], encoding="utf-8")
        print(details["traceback"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_with_diagnostics())
