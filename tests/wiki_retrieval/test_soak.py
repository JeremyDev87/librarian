from __future__ import annotations

import pytest
from pathlib import Path

from local_wiki_librarian.soak import fixed_rate_offsets, validate_query_contract, validate_query_result

ROOT = Path(__file__).resolve().parents[2]


def test_fixed_rate_offsets_are_start_anchored_and_include_end() -> None:
    assert fixed_rate_offsets(3600.0, 300.0) == [float(value) for value in range(0, 3601, 300)]
    assert fixed_rate_offsets(10.0, 4.0) == [0.0, 4.0, 8.0, 10.0]


def test_fixed_rate_offsets_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        fixed_rate_offsets(-1.0, 1.0)
    with pytest.raises(ValueError):
        fixed_rate_offsets(1.0, 0.0)


def test_no_answer_probe_requires_true_empty_results() -> None:
    query = {"query": "absent", "expected_paths": [], "expected_no_answer": True}
    validate_query_result(query, [])
    with pytest.raises(ValueError, match="no-answer"):
        validate_query_result(query, ["domains/unrelated.md"])


def test_query_contract_is_xor() -> None:
    with pytest.raises(ValueError, match="xor"):
        validate_query_contract({"query": "bad", "expected_paths": [], "expected_no_answer": False})


def test_soak_receipt_contains_per_sample_query_path_and_engine_evidence(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys

    state = tmp_path / "state"
    state.mkdir()
    (state / "current.json").write_text(json.dumps({"generation": "gen"}))
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps({"queries": [{"query": "policy", "expected_paths": ["a.md"]}]}))
    fake_cli = tmp_path / "fake-cli"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "cmd=sys.argv[1]\n"
        "if cmd=='health': print(json.dumps({'status':'ok'}))\n"
        "elif cmd=='audit': print(json.dumps({'status':'ok','passed':True}))\n"
        "else: print(json.dumps({'status':'ok','results':[{'path':'a.md','engine':'librarian'}]}))\n"
    )
    fake_cli.chmod(0o755)
    output = tmp_path / "receipt.json"
    result = subprocess.run([
        sys.executable, os.fspath(ROOT / "scripts/run-wiki-soak.py"),
        "--state-root", os.fspath(state),
        "--queries", os.fspath(queries), "--output", os.fspath(output),
        "--duration-seconds", "0", "--interval-seconds", "1", "--cli", os.fspath(fake_cli),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text())
    sample = receipt["samples"][0]
    assert sample["generation"] == "gen"
    assert sample["health_status"] == "ok"
    assert sample["audit_passed"] is True
    assert sample["queries"][0]["paths"] == ["a.md"]
    assert sample["queries"][0]["engines"] == ["librarian"]


def test_soak_receipt_records_generation_change_cause(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys

    state = tmp_path / "state"
    state.mkdir()
    (state / "current.json").write_text(json.dumps({"generation": "gen-a"}))
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps({"queries": [{"query": "policy", "expected_paths": ["a.md"]}]}))
    fake_cli = tmp_path / "fake-cli"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json,pathlib,sys\n"
        "cmd=sys.argv[1]\n"
        "state=pathlib.Path(sys.argv[sys.argv.index('--state-root')+1])\n"
        "marker=state/'mutated'\n"
        "if cmd=='health':\n"
        "  print(json.dumps({'status':'ok'}))\n"
        "  if not marker.exists():\n"
        "    marker.write_text('yes')\n"
        "    (state/'current.json').write_text(json.dumps({'generation':'gen-b'}))\n"
        "elif cmd=='audit': print(json.dumps({'status':'ok','passed':True}))\n"
        "else: print(json.dumps({'status':'ok','results':[{'path':'a.md','engine':'librarian'}]}))\n"
    )
    fake_cli.chmod(0o755)
    output = tmp_path / "receipt.json"
    result = subprocess.run([
        sys.executable, os.fspath(ROOT / "scripts/run-wiki-soak.py"),
        "--state-root", os.fspath(state),
        "--queries", os.fspath(queries), "--output", os.fspath(output),
        "--duration-seconds", "0.1", "--interval-seconds", "0.1", "--cli", os.fspath(fake_cli),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    receipt = json.loads(output.read_text())
    failure = receipt["failures"][0]
    assert failure["reason_code"] == "generation_changed"
    assert failure["expected_generation"] == "gen-a"
    assert failure["observed_generation"] == "gen-b"
