from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import local_wiki_librarian.refresh as refresh_module
from local_wiki_librarian.cli import _audit, _health, _search, load_runtime
from local_wiki_librarian.refresh import refresh_wiki, rollback_generation

ROOT = Path(__file__).resolve().parents[2]
WIKIMAP = ROOT / "src" / "local_wiki_librarian" / "_vendor" / "wikimap" / "wikimap.py"


def _write_wiki(root: Path, title: str) -> None:
    page = root / "knowledge/policies/policy.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\nstatus: active\nauthority: high\ndomain: wiki\n---\n"
        f"# {title}\natomic wikimap refresh policy\n",
        encoding="utf-8",
    )


def _metadata_digest(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        rows.append((path.relative_to(root).as_posix(), stat.st_mode, stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def test_refresh_promotes_complete_generation_and_preserves_source(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    before = _metadata_digest(wiki)

    result = refresh_wiki(wiki, state, WIKIMAP)

    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    generation = state / "snapshots" / result.generation
    assert current["generation"] == result.generation
    assert current["authority_sha256"] == hashlib.sha256((generation / "authority.json").read_bytes()).hexdigest()
    assert current["wikimap_index_sha256"] == hashlib.sha256((generation / ".wikimap/index.db").read_bytes()).hexdigest()
    assert (generation / "authority.json").is_file()
    assert (generation / ".wikimap/index.db").is_file()
    assert _metadata_digest(wiki) == before
    assert not (wiki / ".wikimap").exists()
    assert not (wiki / "MAP.md").exists()

    health = _health(state)
    search = _search("atomic wikimap refresh", state, 5)
    assert health["status"] == "ok"
    assert search["status"] == "ok"
    assert search["results"][0]["path"] == "knowledge/policies/policy.md"
    assert _audit(state)["passed"] is True


def test_refresh_cli_discovers_vendored_wikimap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "CLI Discovery")
    monkeypatch.setattr(refresh_module, "_find_wikimap_path", lambda: WIKIMAP)

    exit_code = refresh_module.main([
        "--wiki-root", str(wiki), "--state-root", str(state),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert (state / "current.json").is_file()


def test_refresh_discovery_rejects_invalid_bundled_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIBRARIAN_WIKIMAP_PATH", raising=False)
    monkeypatch.setattr(refresh_module, "bundled_vendor_root", lambda: tmp_path)

    assert refresh_module._find_wikimap_path() is None


def test_refresh_failure_preserves_current_and_removes_incomplete_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    current_before = (state / "current.json").read_bytes()
    generations_before = {p.name for p in (state / "snapshots").iterdir()}
    _write_wiki(wiki, "Version Two")

    def fail_index(_self):
        raise RuntimeError("injected index failure")

    monkeypatch.setattr(refresh_module.WikimapAdapter, "index", fail_index)
    with pytest.raises(Exception, match="generation preparation failed"):
        refresh_wiki(wiki, state, WIKIMAP)

    assert (state / "current.json").read_bytes() == current_before
    assert {p.name for p in (state / "snapshots").iterdir()} == generations_before == {first.generation}


def test_refresh_refuses_stale_promotion_and_preserves_last_good_generation(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    current_before = (state / "current.json").read_bytes()
    generations_before = {path.name for path in (state / "snapshots").iterdir()}
    page = wiki / "knowledge/policies/policy.md"
    page.chmod(0o000)
    try:
        with pytest.raises(Exception, match="generation preparation failed"):
            refresh_wiki(wiki, state, WIKIMAP)
    finally:
        page.chmod(0o644)

    assert (state / "current.json").read_bytes() == current_before
    assert {path.name for path in (state / "snapshots").iterdir()} == generations_before == {first.generation}


def test_verified_runtime_rejects_promoted_index_tampering(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    result = refresh_wiki(wiki, state, WIKIMAP)
    index = state / "snapshots" / result.generation / ".wikimap/index.db"
    index.write_bytes(index.read_bytes() + b"tamper")

    with pytest.raises(RuntimeError, match="wikimap index integrity"):
        load_runtime(state)


def test_two_generation_rollback_restores_previous_search(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    _write_wiki(wiki, "Version Two")
    second = refresh_wiki(wiki, state, WIKIMAP)
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert current["generation"] == second.generation
    assert current["previous_generation"] == first.generation
    first_dir = state / "snapshots" / first.generation
    assert current["previous_manifest_sha256"] == hashlib.sha256(
        (first_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert current["previous_authority_sha256"] == hashlib.sha256(
        (first_dir / "authority.json").read_bytes()
    ).hexdigest()
    assert current["previous_wikimap_index_sha256"] == hashlib.sha256(
        (first_dir / ".wikimap/index.db").read_bytes()
    ).hexdigest()

    rolled_back = rollback_generation(state, first.generation)
    assert rolled_back.generation == first.generation
    after = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert after["generation"] == first.generation
    assert after["previous_generation"] == second.generation
    second_dir = state / "snapshots" / second.generation
    assert after["previous_manifest_sha256"] == hashlib.sha256(
        (second_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert after["previous_authority_sha256"] == hashlib.sha256(
        (second_dir / "authority.json").read_bytes()
    ).hexdigest()
    assert after["previous_wikimap_index_sha256"] == hashlib.sha256(
        (second_dir / ".wikimap/index.db").read_bytes()
    ).hexdigest()

    reversed_rollback = rollback_generation(state, second.generation)
    assert reversed_rollback.generation == second.generation
    reversed_pointer = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert reversed_pointer["generation"] == second.generation
    assert reversed_pointer["previous_generation"] == first.generation
    assert reversed_pointer["previous_manifest_sha256"] == hashlib.sha256(
        (first_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert reversed_pointer["previous_authority_sha256"] == hashlib.sha256(
        (first_dir / "authority.json").read_bytes()
    ).hexdigest()
    assert reversed_pointer["previous_wikimap_index_sha256"] == hashlib.sha256(
        (first_dir / ".wikimap/index.db").read_bytes()
    ).hexdigest()

    search = _search("Version Two", state, 5)
    assert search["status"] == "ok"
    assert search["results"][0]["path"] == "knowledge/policies/policy.md"
