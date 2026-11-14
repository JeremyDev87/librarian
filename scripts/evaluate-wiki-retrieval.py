#!/usr/bin/env python3
"""Measure constructed inline corpora without changing the retrieval policy."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata

from local_wiki_librarian.cli import load_operational_runtime
from local_wiki_librarian.lexical import WikimapAdapter
from local_wiki_librarian.paths import read_bytes_within, resolve_within
from local_wiki_librarian.refresh import refresh_wiki
from local_wiki_librarian.router import RouterConfig, WikiRouter
from local_wiki_librarian.scorer import aggregate_reports, score_query
from local_wiki_librarian.soak import validate_query_contract
from local_wiki_librarian.vendor import bundled_wikimap_path, verify_vendor


def digest(value) -> str:
    payload = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def relative_path(value) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value and ":" not in value
            and not any(ord(char) < 32 for char in value) and not value.startswith("/")
            and all(part not in {"", ".", ".."} for part in value.split("/"))
            and str(PurePosixPath(value)) == value)


def validate_corpus(data) -> dict:
    required = {"schema_version", "corpus_id", "source_revision", "provenance", "documents", "queries"}
    if not isinstance(data, dict) or set(data) != required or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("corpus_schema")
    if data["corpus_id"] != "librarian-public-seed-v1" or not isinstance(data["source_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", data["source_revision"]):
        raise ValueError("corpus_identity")
    if any(not isinstance(data[key], list) or not data[key] for key in ("documents", "queries", "provenance")):
        raise ValueError("empty_collection")
    paths, aliases = set(), set()
    for doc in data["documents"]:
        if not isinstance(doc, dict) or set(doc) != {"path", "content"} or not relative_path(doc["path"]) or not doc["path"].endswith(".md") or not isinstance(doc["content"], str) or not doc["content"].strip():
            raise ValueError("inline_document")
        alias = unicodedata.normalize("NFC", doc["path"]).casefold()
        if alias in aliases:
            raise ValueError("duplicate_document")
        paths.add(doc["path"])
        aliases.add(alias)
    provenance_paths = set()
    for item in data["provenance"]:
        if not isinstance(item, dict) or set(item) != {"document_path", "source_path", "section", "source_sha256", "kind", "description"} or not all(isinstance(value, str) and value.strip() for value in item.values()):
            raise ValueError("provenance_schema")
        if item["document_path"] not in paths or not relative_path(item["source_path"]) or item["kind"] not in {"synthetic", "public-derived"} or not re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"]):
            raise ValueError("provenance_contract")
        provenance_paths.add(item["document_path"])
    if provenance_paths != paths:
        raise ValueError("provenance_coverage")
    ids, families = set(), {}
    fields = {"id", "family", "split", "query", "should_not_return", "label_basis", "variants"}
    for query in data["queries"]:
        if not isinstance(query, dict) or not fields.issubset(query) or set(query) - fields not in ({"expected_paths"}, {"expected_no_answer"}):
            raise ValueError("query_schema")
        if any(not isinstance(query[key], str) or not query[key].strip() for key in ("id", "family", "split", "query", "label_basis")):
            raise ValueError("query_text")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", query[key]) for key in ("id", "family")) or query["id"] in ids or query["split"] not in {"dev", "frozen_check"}:
            raise ValueError("query_identity")
        ids.add(query["id"])
        if families.setdefault(query["family"], query["split"]) != query["split"]:
            raise ValueError("family_split")
        for key in ("expected_paths", "should_not_return", "variants"):
            values = query.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values) or len(set(values)) != len(values):
                raise ValueError("query_list")
            if key != "variants" and not set(values).issubset(paths):
                raise ValueError("unknown_label")
        validate_query_contract(query)
        if set(query.get("expected_paths", [])).intersection(query["should_not_return"]):
            raise ValueError("contradictory_label")
    return data


def inventory(source: Path) -> list[dict]:
    return [{"path": path.relative_to(source).as_posix(), "sha256": digest(path.read_bytes()),
             "mode": stat.S_IMODE(path.stat().st_mode), "mtime_ns": path.stat().st_mtime_ns,
             "size": path.stat().st_size} for path in sorted(source.rglob("*")) if path.is_file()]


def prepare(corpus: dict, root: Path) -> tuple:
    source, state = root / "source", root / "state"
    source.mkdir()
    for doc in corpus["documents"]:
        path = resolve_within(source, doc["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(doc["content"])
    before = inventory(source)
    vendor = bundled_wikimap_path()
    verify_vendor(vendor.parent)
    refresh_wiki(source, state, vendor)
    snapshot, authority, runtime_vendor = load_operational_runtime(state)
    snapshot = snapshot.resolve()
    if runtime_vendor.resolve() != vendor.resolve():
        raise RuntimeError("bundled_vendor_required")
    router = WikiRouter(WikimapAdapter(vendor, snapshot), authority, RouterConfig(k=5, graph_depth=1))
    pointer = {"sha256": digest((state / "current.json").read_bytes()), "generation": authority.generation,
               "index_sha256": digest((snapshot / ".wikimap/index.db").read_bytes()), "source_before": before}
    return router, state, pointer


def check_runtime(runtime: tuple) -> None:
    router, state, pointer = runtime
    if digest(read_bytes_within(state, "current.json")) != pointer["sha256"]:
        raise RuntimeError("generation_changed")
    original_index = Path("snapshots") / pointer["generation"] / ".wikimap/index.db"
    if digest(read_bytes_within(state, original_index)) != pointer["index_sha256"] or digest(read_bytes_within(router.adapter.snapshot_dir, ".wikimap/index.db")) != pointer["index_sha256"]:
        raise RuntimeError("index_integrity_failed")


def check_cli(corpus: dict, runtime: tuple) -> list[dict]:
    router, state, _ = runtime
    direct = WikiRouter(router.adapter, router.authority, RouterConfig(k=5, graph_depth=1))
    positive = next((row["query"] for row in corpus["queries"] if row.get("expected_paths")), corpus["queries"][0]["query"])
    field = next((row["query"] for row in corpus["queries"] if row["query"].startswith("title:")), "title:" + positive)
    checks = []
    env = dict(os.environ, LIBRARIAN_WIKIMAP_PATH=str(bundled_wikimap_path()))
    env.pop("PYTHONPATH", None)
    for kind, query in (("positive", positive), ("negative", "__evaluation_absent_" + digest(corpus)), ("field", field)):
        expected = [result.path for result in direct.search(query)]
        result = subprocess.run([sys.executable, "-m", "local_wiki_librarian.cli", "search", query,
                                 "--state-root", str(state), "-n", "5", "--graph-hops", "1"],
                                capture_output=True, text=True, timeout=60, check=True, env=env)
        payload = json.loads(result.stdout)
        if payload.get("status") not in {"ok", "degraded"} or [item["path"] for item in payload["results"]] != expected:
            raise RuntimeError("cli_path_mismatch")
        checks.append({"kind": kind, "query_sha256": digest(query), "paths": expected, "matched": True, "k": 5})
    return checks


def failure(exc: Exception) -> dict:
    known = {"generation_changed", "index_integrity_failed", "nondeterministic_result", "source_changed", "cli_path_mismatch"}
    return {"exception_class": type(exc).__name__, "reason_code": str(exc) if str(exc) in known else "execution_error"}


def aggregate_rows(rows: list[dict], ineligible: list[str]) -> dict:
    groups = {"all": rows}
    groups.update({split: [row for row in rows if row["split"] == split] for split in ("dev", "frozen_check")})
    groups.update({label: [row for row in rows if row["expected_no_answer"] == no_answer]
                   for label, no_answer in (("answerable", False), ("no_answer", True))})
    reports = {}
    for name, group in groups.items():
        scores = [score_query(row["id"], row["expected_paths"], row["paths"], ineligible, k=5,
                              expected_no_answer=row["expected_no_answer"]) for row in group]
        reports[name] = {key: value for key, value in aggregate_reports("librarian", scores).to_dict().items() if key != "per_query"}
        reports[name]["forbidden_return_count"] = sum(bool(row["forbidden_paths"]) for row in group)
    return reports


def evaluate(corpus: dict, runtime: tuple) -> list[dict]:
    router, _, _ = runtime
    ineligible = [entry.relative_path for entry in router.authority.entries if entry.tier not in {"current", "authority"} or entry.do_not_answer_as_current]
    rows = []
    for query in corpus["queries"]:
        row = {"id": query["id"], "family": query["family"], "split": query["split"],
               "query_sha256": digest(query["query"]), "label_sha256": digest({key: query[key] for key in ("expected_paths", "expected_no_answer", "should_not_return", "label_basis") if key in query}),
               "variants_sha256": digest(query["variants"]), "expected_paths": query.get("expected_paths", []),
               "expected_no_answer": query.get("expected_no_answer", False), "status": "invalid", "outcome": "execution_error",
               "paths": [], "samples": [], "warmup_count": 0, "diagnostics": None, "score": None, "error": None}
        try:
            check_runtime(runtime)
            expected = None
            for iteration in range(6):
                start = time.perf_counter_ns()
                results = router.search(query["query"])
                elapsed = (time.perf_counter_ns() - start) / 1_000_000
                diagnostics = {"weak": router.adapter.last_weak, "partial": router.adapter.last_partial,
                               "terms": [{"term_sha256": digest(term.get("term", "")), **{key: term[key] for key in ("df", "idf") if type(term.get(key)) in {int, float}}} for term in router.adapter.last_terms],
                               "warnings": ["dead_vocabulary" if warning == "weak lexical query contains dead vocabulary; returning no answer" else "graph_expansion_failed" if warning.startswith("graph expansion failed for ") else "unknown" for warning in router.last_warnings]}
                paths = [result.path for result in results]
                if any(path not in {entry.relative_path for entry in router.authority.entries} for path in paths):
                    raise RuntimeError("unknown_result_path")
                if expected is not None and paths != expected:
                    raise RuntimeError("nondeterministic_result")
                expected = paths
                if iteration == 0:
                    row["warmup_count"] = 1
                    continue
                row["samples"].append({"latency_ms": elapsed, "paths": paths, "diagnostics": diagnostics,
                                       "results": [{"path": item.path, "tier": item.tier, "source": item.source, "graph_distance": item.graph_distance} for item in results]})
            check_runtime(runtime)
            score = score_query(query["id"], row["expected_paths"], paths, ineligible, k=5, expected_no_answer=row["expected_no_answer"])
            samples = [sample["latency_ms"] for sample in row["samples"]]
            row.update(status="complete", paths=paths, diagnostics=diagnostics,
                       score={key: value for key, value in score.to_dict().items() if key != "query"},
                       forbidden_paths=sorted(set(paths).intersection(query["should_not_return"])),
                       latency_ms={"median": statistics.median(samples), "p95": max(samples)},
                       outcome=("abstained" if not paths else "wrong_candidate") if row["expected_no_answer"] else ("hit" if score.recall_at_k else "miss"))
        except Exception as exc:
            # Each query is an execution boundary: never score an exception as an empty answer.
            row.update(error=failure(exc), diagnostics=None)
        rows.append(row)
    return rows


def run(corpus: dict) -> dict:
    report = {"schema_version": 1, "corpus_id": corpus["corpus_id"], "status": "invalid", "aggregate": None,
              "package_version": version("local-wiki-librarian"), "vendor_sha256": digest(bundled_wikimap_path().read_bytes()),
              "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "os": platform.system()},
              "measurement": {"concurrency": 1, "k": 5, "graph_depth": 1, "include_zero_score_graph": False,
                              "warmups": 1, "repetitions": 5, "clock": "perf_counter_ns", "scope": "router.search only",
                              "p95_method": "nearest-rank; five-sample observed maximum; not population latency or SLA",
                              "mrr_definition": "existing scorer: reciprocal first relevant rank; correct abstention = 1",
                              "corpus_scope": "constructed public seed; not independent real-user holdout"},
              "per_query": [], "error": None}
    with tempfile.TemporaryDirectory(prefix="librarian-evaluation-") as owned:
        root = Path(owned).resolve()
        try:
            runtime = prepare(corpus, root)
            router, _, pointer = runtime
            report.update(generation=pointer["generation"], current_pointer_sha256=pointer["sha256"], source_before=pointer["source_before"])
            report["cli_checks"] = check_cli(corpus, runtime)
            report["per_query"] = evaluate(corpus, runtime)
            check_runtime(runtime)
            ineligible = [entry.relative_path for entry in router.authority.entries if entry.tier not in {"current", "authority"} or entry.do_not_answer_as_current]
            report["authority_ineligible_paths"] = sorted(ineligible)
            if all(row["status"] == "complete" for row in report["per_query"]):
                report.update(status="complete", aggregate=aggregate_rows(report["per_query"], ineligible))
        except Exception as exc:
            report["error"] = failure(exc)
        finally:
            report["source_after"] = inventory(root / "source")
            report["source_preserved"] = report.get("source_before") == report["source_after"]
            report["source_inventory_sha256"] = digest(report["source_after"])
        if not report["source_preserved"]:
            report.update(status="invalid", aggregate=None, error={"reason_code": "source_changed"})
        if not report["per_query"]:
            report["per_query"] = [{"id": query["id"], "status": "invalid", "outcome": "execution_error", "diagnostics": None,
                                    "error": report["error"], "samples": [], "score": None} for query in corpus["queries"]]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.corpus.is_symlink() or not args.corpus.is_file() or args.output.exists() or args.output.is_symlink():
            raise ValueError("input_or_output_path")
        payload = args.corpus.read_bytes()
        corpus = validate_corpus(json.loads(payload.decode("utf-8")))
        os.environ["LIBRARIAN_WIKIMAP_PATH"] = str(bundled_wikimap_path())
        report = run(corpus)
        report["corpus_sha256"] = digest(payload)
        if args.corpus.read_bytes() != payload:
            report.update(status="invalid", aggregate=None, error={"reason_code": "corpus_changed"})
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"status": report["status"], "query_count": len(report["per_query"])}))
        return 0 if report["status"] == "complete" else 2
    except Exception as exc:
        print(json.dumps({"status": "invalid", "error": failure(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
