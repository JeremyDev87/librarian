from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/evaluate-wiki-retrieval.py"


@pytest.fixture
def corpus():
    documents = [
        {"path": "current/a.md", "content": "---\ntitle: Albatross\nauthority: high\nstatus: active\n---\n# Albatross\nAlbatross migration.\n"},
        {"path": "current/b.md", "content": "---\ntitle: Badger\nauthority: high\nstatus: active\n---\n# Badger\nBadger habitat.\n"},
        {"path": "current/blocked.md", "content": "---\nauthority: high\nstatus: active\ndo_not_answer_as_current: true\n---\nAlbatross\n"},
        {"path": "raw/a.md", "content": "# Albatross\nAlbatross\n"},
    ]
    queries = []
    for case_id, query, label in [
        ("first", "Albatross", {"expected_paths": ["current/b.md"]}),
        ("middle", "Badger", {"expected_no_answer": True}),
        ("negative", "unfindablequasar", {"expected_no_answer": True}),
        ("last", "title:Albatross", {"expected_paths": ["current/a.md"]}),
    ]:
        queries.append({"id": case_id, "family": case_id, "split": "dev" if case_id in {"first", "middle"} else "frozen_check", "query": query, **label,
                        "should_not_return": ["raw/a.md", "current/blocked.md"], "label_basis": "Synthetic evaluation control", "variants": []})
    return {"schema_version": 1, "corpus_id": "librarian-public-seed-v1", "source_revision": "0" * 40,
            "documents": documents, "queries": queries,
            "provenance": [{"document_path": doc["path"], "source_path": "README.md", "section": "control", "source_sha256": "0" * 64, "kind": "synthetic", "description": "Synthetic control"} for doc in documents]}


def run_evaluation(tmp_path, corpus, patch=""):
    source = tmp_path / "corpus.json"
    source.write_text(json.dumps(corpus), encoding="utf-8")
    source_before = (source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns)
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    output = tmp_path / "report.json"
    env = dict(os.environ, TMPDIR=str(runtime), PYTHONDONTWRITEBYTECODE="1", LIBRARIAN_WIKIMAP_PATH="/not/the/bundled/vendor.py")
    command = [sys.executable, str(SCRIPT)]
    if patch:
        runner = tmp_path / "runner.py"
        runner.write_text("import runpy, sys\nfrom pathlib import Path\nimport json\nns = runpy.run_path(" + repr(str(SCRIPT)) + ")['main'].__globals__\n" + patch + "\nraise SystemExit(ns['main']())\n", encoding="utf-8")
        command = [sys.executable, str(runner)]
    result = subprocess.run([*command, "--corpus", str(source), "--output", str(output)], capture_output=True, text=True, env=env, timeout=90)
    assert list(runtime.iterdir()) == [], "process exit must remove owned source/state and verified views"
    assert (source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns) == source_before
    return result, json.loads(output.read_text()) if output.is_file() else None


def test_real_cli_preserves_misses_and_scores_all_rows(tmp_path, corpus):
    result, report = run_evaluation(tmp_path, corpus)
    assert result.returncode == 0, result.stderr
    assert report["status"] == "complete"
    rows = report["per_query"]
    assert [row["id"] for row in rows] == ["first", "middle", "negative", "last"]
    assert [row["outcome"] for row in rows] == ["miss", "wrong_candidate", "abstained", "hit"]
    assert report["aggregate"]["all"]["avg_recall_at_k"] == 0.5
    assert report["aggregate"]["all"]["query_count"] == 4
    assert report["aggregate"]["dev"]["avg_recall_at_k"] == 0
    assert report["aggregate"]["frozen_check"]["avg_recall_at_k"] == 1
    assert report["source_preserved"] is True
    assert report["source_before"] == report["source_after"]
    assert {row["kind"] for row in report["cli_checks"]} == {"positive", "negative", "field"}
    assert all(row["matched"] and row["k"] == 5 for row in report["cli_checks"])
    assert all(len(row["samples"]) == 5 and row["warmup_count"] == 1 for row in rows)
    assert all(row["latency_ms"]["p95"] == max(sample["latency_ms"] for sample in row["samples"]) for row in rows)
    assert rows[0]["samples"][0]["diagnostics"]["terms"]
    encoded = json.dumps(report)
    assert "Albatross" not in encoded and "unfindablequasar" not in encoded
    assert str(tmp_path) not in encoded and "/not/the/" not in encoded


@pytest.mark.parametrize("damage", ["empty", "type", "escape", "duplicate", "duplicate_label", "unknown_label", "both_labels", "partial_row", "alias", "provenance", "family_split"])
def test_invalid_corpus_is_rejected_before_preparation(tmp_path, corpus, damage):
    bad = copy.deepcopy(corpus)
    if damage == "empty":
        bad["queries"] = []
    elif damage == "type":
        bad["documents"][0]["content"] = 42
    elif damage == "escape":
        bad["documents"][0]["path"] = "../escape.md"
    elif damage == "duplicate":
        bad["queries"][1]["id"] = "first"
    elif damage == "duplicate_label":
        bad["queries"][0]["expected_paths"] *= 2
    elif damage == "unknown_label":
        bad["queries"][0]["expected_paths"] = ["missing.md"]
    elif damage == "both_labels":
        bad["queries"][0]["expected_no_answer"] = True
    elif damage == "partial_row":
        del bad["queries"][0]["label_basis"]
    elif damage == "alias":
        bad["documents"].append({"path": "CURRENT/A.md", "content": "Alias"})
    elif damage == "provenance":
        bad["provenance"] = []
    else:
        bad["queries"][2]["family"] = "first"
    result, report = run_evaluation(tmp_path, bad)
    assert result.returncode == 2
    assert report is None


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_existing_output_is_never_overwritten(tmp_path, corpus, kind):
    output = tmp_path / "report.json"
    target = tmp_path / "target.json"
    if kind == "file":
        output.write_text('{"sentinel": true}')
    elif kind == "symlink":
        target.write_text('{"sentinel": true}')
        output.symlink_to(target)
    else:
        output.symlink_to(target)
    result, _ = run_evaluation(tmp_path, corpus)
    assert result.returncode == 2
    assert output.is_symlink() if kind != "file" else json.loads(output.read_text()) == {"sentinel": True}
    if kind == "symlink":
        assert target.read_text() == '{"sentinel": true}'
    if kind == "dangling":
        assert not target.exists()


@pytest.mark.parametrize("fault", ["exception", "missing_index", "corrupt_index", "state_index_missing", "state_index_corrupt", "drift", "nondeterministic", "source_changed", "cli_failure"])
def test_failures_are_invalid_without_success_aggregate(tmp_path, corpus, fault):
    corpus["queries"] = [corpus["queries"][0], corpus["queries"][-1]]
    patch = """
original = ns['prepare']
def prepare(corpus, root):
    runtime = original(corpus, root)
    router, state, pointer = runtime
    search = router.search
    count = [0]
    def broken(query):
        count[0] += 1
        if count[0] == 2:
            FAULT
        return search(query)
    router.search = broken
    return runtime
ns['prepare'] = prepare
"""
    actions = {
        "exception": "raise RuntimeError('/private/raw-query-secret')",
        "missing_index": "(router.adapter.snapshot_dir / '.wikimap/index.db').unlink()",
        "corrupt_index": "(router.adapter.snapshot_dir / '.wikimap/index.db').write_bytes(b'corrupt')",
        "state_index_missing": "index = state / 'snapshots' / pointer['generation'] / '.wikimap/index.db'; index.parent.chmod(0o755); index.unlink()",
        "state_index_corrupt": "index = state / 'snapshots' / pointer['generation'] / '.wikimap/index.db'; index.chmod(0o644); index.write_bytes(b'corrupt')",
        "drift": "(state / 'current.json').write_text('{}')",
        "nondeterministic": "return []",
        "source_changed": "(root / 'source/current/a.md').write_text('changed')",
        "cli_failure": "raise AssertionError('unused')",
    }
    if fault == "cli_failure":
        patch = "def fail(*args):\n    raise subprocess.CalledProcessError(7, 'hidden')\nimport subprocess\nns['check_cli'] = fail\n"
    else:
        patch = patch.replace("FAULT", actions[fault])
    result, report = run_evaluation(tmp_path, corpus, patch)
    assert result.returncode == 2, result.stderr
    assert report["status"] == "invalid" and report["aggregate"] is None
    assert len(report["per_query"]) == 2
    assert "raw-query-secret" not in json.dumps(report)
    if fault not in {"source_changed", "cli_failure"}:
        assert report["per_query"][0]["diagnostics"] is None
        assert report["per_query"][0]["error"]["reason_code"]
    if fault in {"exception", "nondeterministic"}:
        assert report["per_query"][-1]["status"] == "complete"


def test_forbidden_and_ineligible_candidates_are_counted(tmp_path, corpus):
    corpus["queries"] = [corpus["queries"][0], corpus["queries"][-1]]
    patch = """
original = ns['prepare']
def prepare(corpus, root):
    runtime = original(corpus, root)
    runtime[0].config = ns['RouterConfig'](k=5, graph_depth=1, authority_filter=False)
    return runtime
ns['prepare'] = prepare
ns['check_cli'] = lambda *args: []
"""
    result, report = run_evaluation(tmp_path, corpus, patch)
    assert result.returncode == 0, result.stderr
    assert report["per_query"][0]["forbidden_paths"] == ["current/blocked.md", "raw/a.md"]
    assert report["per_query"][0]["score"]["authority_false_positive"] is True
    assert report["aggregate"]["all"]["authority_false_positive_count"] == 2


def test_malformed_json_and_regular_file_requirement(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text('{"malformed":')
    result = subprocess.run([sys.executable, str(SCRIPT), "--corpus", str(bad), "--output", str(tmp_path / "out.json")], capture_output=True, text=True, env=dict(os.environ, TMPDIR=str(runtime)), timeout=10)
    assert result.returncode == 2
    assert not (tmp_path / "out.json").exists() and list(runtime.iterdir()) == []


def test_real_cli_matches_router_when_more_than_five_documents_match(tmp_path, corpus):
    corpus["documents"] = [{"path": f"current/item-{index}.md", "content": "---\ntitle: Commonneedle\nauthority: high\nstatus: active\n---\nCommonneedle\n"} for index in range(6)]
    corpus["provenance"] = [{**corpus["provenance"][0], "document_path": doc["path"]} for doc in corpus["documents"]]
    corpus["queries"] = [{**corpus["queries"][0], "query": "Commonneedle", "expected_paths": [doc["path"] for doc in corpus["documents"]], "should_not_return": []}]
    result, report = run_evaluation(tmp_path, corpus)
    assert result.returncode == 0, result.stderr
    assert len(report["per_query"][0]["paths"]) == 5
    assert report["aggregate"]["all"]["avg_recall_at_k"] == 0.8333
    assert [len(check["paths"]) for check in report["cli_checks"]] == [5, 0, 5]


@pytest.mark.parametrize("attempt", [1, 2])
def test_interrupted_process_removes_owned_resources(tmp_path, corpus, attempt):
    patch = """
original = ns['evaluate']
def interrupted(corpus, runtime):
    runtime[0].search(corpus['queries'][0]['query'])
    raise KeyboardInterrupt
ns['evaluate'] = interrupted
"""
    result, report = run_evaluation(tmp_path, corpus, patch)
    assert result.returncode != 0 and "KeyboardInterrupt" in result.stderr
    assert report is None


def test_tempdir_alias_uses_canonical_owned_root(tmp_path, corpus):
    actual = tmp_path / "actual-runtime"
    actual.mkdir()
    (tmp_path / "runtime").symlink_to(actual, target_is_directory=True)
    corpus["queries"] = [corpus["queries"][-1]]
    result, report = run_evaluation(tmp_path, corpus)
    assert result.returncode == 0, result.stderr
    assert report["status"] == "complete" and report["source_preserved"] is True
