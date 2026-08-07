#!/usr/bin/env python3
"""Run a generation-bound, fixed-rate Local Wiki Librarian soak."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from local_wiki_librarian.soak import fixed_rate_offsets, validate_query_contract, validate_query_result  # noqa: E402


def _run(cli: str, args: list[str], env: dict[str, str], timeout: float) -> dict:
    result = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout, check=False, env=env)
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"command failed with exit {result.returncode}")
    return json.loads(result.stdout)


def _generation(state_root: Path) -> str:
    return str(json.loads((state_root / "current.json").read_text(encoding="utf-8"))["generation"])


def _failure_evidence(exc: BaseException, state_root: Path, expected: str) -> dict[str, object]:
    try:
        observed = _generation(state_root)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        observed = "unavailable"
    if observed != expected:
        return {"error_type": type(exc).__name__, "reason_code": "generation_changed",
                "expected_generation": expected, "observed_generation": observed}
    if isinstance(exc, subprocess.TimeoutExpired):
        code = "command_timeout"
    elif isinstance(exc, json.JSONDecodeError):
        code = "invalid_command_json"
    elif "health or audit" in str(exc):
        code = "health_or_audit_failed"
    elif "forbidden authority path" in str(exc):
        code = "forbidden_authority_path"
    elif "retrieval failed" in str(exc):
        code = "query_retrieval_failed"
    elif isinstance(exc, ValueError):
        code = "query_contract_failed"
    else:
        code = "runtime_failure"
    return {"error_type": type(exc).__name__, "reason_code": code}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed-rate Local Wiki Librarian soak")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--queries", required=True, help="Query manifest")
    parser.add_argument("--output", required=True, help="Compact receipt JSON")
    parser.add_argument("--duration-seconds", type=float, default=3600.0)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--command-timeout", type=float, default=60.0)
    parser.add_argument("--cli", default="wiki-librarian")
    args = parser.parse_args()
    state_root = Path(args.state_root).resolve()
    query_path = Path(args.queries).resolve()
    queries = list(json.loads(query_path.read_text(encoding="utf-8")).get("queries", []))
    if not queries:
        print("error: query corpus is empty", file=sys.stderr)
        return 2
    for query in queries:
        validate_query_contract(query)
    generation = _generation(state_root)
    offsets = fixed_rate_offsets(args.duration_seconds, args.interval_seconds)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["LIBRARIAN_STATE"] = str(state_root)
    started_wall, started = datetime.now(timezone.utc), time.monotonic()
    failures, samples, engines_seen = [], [], set()
    completed = 0
    for sample_index, offset in enumerate(offsets):
        delay = started + offset - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        query_details = []
        try:
            if _generation(state_root) != generation:
                raise RuntimeError("snapshot generation changed during soak")
            health = _run(args.cli, ["health", "--state-root", str(state_root)], env, args.command_timeout)
            audit = _run(args.cli, ["audit", "--state-root", str(state_root)], env, args.command_timeout)
            if health.get("status") != "ok" or audit.get("passed") is not True:
                raise RuntimeError("health or audit gate failed")
            for query_index, query in enumerate(queries):
                report = _run(args.cli, ["search", str(query["query"]), "--state-root", str(state_root), "-n", "5"], env, args.command_timeout)
                if report.get("status") == "error":
                    raise RuntimeError(f"query {query_index} retrieval failed")
                results = list(report.get("results", []))
                paths = [str(item.get("canonical_path") or item.get("path") or "") for item in results]
                engines = sorted({str(item.get("engine")) for item in results if item.get("engine")})
                engines_seen.update(engines)
                validate_query_result(query, paths)
                forbidden = [path for path in paths[:5] if path in set(query.get("should_not_return", []))]
                if forbidden:
                    raise RuntimeError(f"query {query_index} returned forbidden authority path")
                query_details.append({"query_index": query_index, "query": str(query["query"]),
                    "query_sha256": hashlib.sha256(str(query["query"]).encode()).hexdigest(),
                    "paths": paths, "engines": engines})
            completed += 1
            samples.append({"sample": sample_index, "generation": generation, "status": "ok",
                "health_status": health.get("status"), "audit_passed": True, "queries": query_details})
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            failure = {"sample": sample_index, **_failure_evidence(exc, state_root, generation)}
            failures.append(failure)
            samples.append({"sample": sample_index, "generation": generation, "status": "failed",
                **{k: v for k, v in failure.items() if k != "sample"}, "queries": query_details})
    ended = time.monotonic()
    receipt = {"schema_version": 2, "generation": generation,
        "started_at": started_wall.isoformat().replace("+00:00", "Z"),
        "ended_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(ended - started, 3), "requested_duration_seconds": args.duration_seconds,
        "interval_seconds": args.interval_seconds, "sample_count": completed,
        "expected_sample_count": len(offsets), "failure_count": len(failures),
        "passed": completed == len(offsets) and not failures, "engines_seen": sorted(engines_seen),
        "query_count": len(queries), "queries_sha256": hashlib.sha256(query_path.read_bytes()).hexdigest(),
        "failures": failures, "samples": samples}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("generation", "duration_seconds", "sample_count", "expected_sample_count", "failure_count", "passed", "engines_seen", "query_count")}, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
