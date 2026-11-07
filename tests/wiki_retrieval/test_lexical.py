"""Tests for the read-only wikimap lexical adapter."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.lexical import (  # noqa: E402
    ALLOWED_COMMANDS,
    BLOCKED_COMMANDS,

    WikimapAdapter,
    WikimapAdapterError,
)

WIKIMAP_PATH = ROOT / "src" / "local_wiki_librarian" / "_vendor" / "wikimap" / "wikimap.py"


def _make_mini_wiki(snapshot_dir: Path) -> None:
    """Create a small wiki fixture inside a snapshot directory."""
    (snapshot_dir / "knowledge/policies").mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "knowledge/policies/policy.md").write_text(
        "---\nwiki_schema: librarian-v1\nauthority: high\nstatus: active\n---\n\n"
        "# Local Index Policy\n\nContent about split index policy.\n",
        encoding="utf-8",
    )
    (snapshot_dir / "domains").mkdir(exist_ok=True)
    (snapshot_dir / "domains/wiki-guide.md").write_text(
        "---\ntitle: Wiki Guide\n---\n\n# Wiki Guide\n\nGeneral wiki operations.\n",
        encoding="utf-8",
    )


class TestAdapterConstruction:
    def test_creates_adapter(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        assert adapter.snapshot_dir == snapshot.resolve()

    def test_missing_wikimap_raises(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        with pytest.raises(WikimapAdapterError, match="wikimap.py not found"):
            WikimapAdapter(tmp_path / "nonexistent.py", snapshot)

    def test_missing_snapshot_raises(self, tmp_path: Path) -> None:
        with pytest.raises(WikimapAdapterError, match="snapshot directory not found"):
            WikimapAdapter(WIKIMAP_PATH, tmp_path / "nonexistent")


class TestSafetyGuards:
    def test_blocked_commands_rejected(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        for cmd in ("install", "mv", "link", "note", "embed", "migrate", "edge"):
            with pytest.raises(WikimapAdapterError, match="blocked"):
                adapter._run(cmd, [])

    def test_unknown_command_rejected(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        with pytest.raises(WikimapAdapterError, match="unknown or disallowed"):
            adapter._run("dangerous_command", [])

    def test_allowed_commands_set(self) -> None:
        assert "update" in ALLOWED_COMMANDS
        assert "search" in ALLOWED_COMMANDS
        assert "links" in ALLOWED_COMMANDS
        assert "path" in ALLOWED_COMMANDS

    def test_install_is_blocked(self) -> None:
        assert "install" in BLOCKED_COMMANDS

    def test_mv_is_blocked(self) -> None:
        assert "mv" in BLOCKED_COMMANDS


class TestIndexAndSearch:
    @pytest.mark.parametrize(
        "payload",
        [
            "{}",
            '{"results": []}',
            '{"query": "wrong", "results": [], "terms": [], "weak": false, "partial": false}',
        ],
    )
    def test_search_rejects_incomplete_or_mismatched_json_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str,
    ) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        monkeypatch.setattr(adapter, "_run", lambda *_args, **_kwargs: payload)

        with pytest.raises(WikimapAdapterError, match="invalid response schema"):
            adapter.search("health-probe", n=1)

    def test_index_creates_no_map_md(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        assert not (snapshot / "MAP.md").exists(), "MAP.md must not be created"

    def test_search_returns_results(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        results = adapter.search("policy", n=5)
        assert len(results) > 0
        # The policy doc should be in top results
        top_files = [r.file for r in results[:3]]
        assert any("policy" in f for f in top_files)

    def test_search_returns_scores(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        results = adapter.search("wiki guide", n=5)
        for r in results:
            assert isinstance(r.score, float)

    def test_search_exposes_weak_dead_vocabulary(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        adapter.search("목성 nonexistent-wiki-term", n=5)
        assert adapter.last_weak is True
        assert any(item.get("df") == 0 for item in adapter.last_terms)

    def test_search_text_output(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        text = adapter.search_text("wiki", n=3)
        assert isinstance(text, str)
        assert len(text) > 0


class TestRegression:
    def test_no_map_md_after_multiple_operations(self, tmp_path: Path) -> None:
        """Regression: canonical MAP.md must never appear after any adapter operation."""
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        adapter.search("test", n=3)
        assert not (snapshot / "MAP.md").exists()

    def test_no_writes_outside_snapshot(self, tmp_path: Path) -> None:
        """Regression: no files created outside the snapshot directory."""
        snapshot = tmp_path / "snapshot"
        parent = snapshot.parent
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()

        # Check no MAP.md escaped to parent
        assert not (parent / "MAP.md").exists()
        # Check no escaped.md pattern from --map-path vulnerability
        assert not (parent / "escaped.md").exists()


@pytest.fixture(scope="module", params=[499, 500])
def boundary_index(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> WikimapAdapter:
    snapshot = tmp_path_factory.mktemp(f"query-boundary-{request.param}")
    (snapshot / "park.md").write_text(
        "---\ntitle: Park policy\ntags: [outdoors]\n---\n"
        "# Walking\n\n공원 산책. exact phrase and literalneedle.\n"
        "## Restricted\n\nNo destination here.\n", encoding="utf-8",
    )
    for index in range(request.param - 1):
        (snapshot / f"filler-{index:03}.md").write_text(
            "# Filler\n\nUnrelated material.\n", encoding="utf-8",
        )
    adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
    adapter.index()
    with sqlite3.connect(snapshot / ".wikimap/index.db") as db:
        assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == request.param
        has_fts = db.execute("SELECT 1 FROM sqlite_master WHERE name='sections_fts'").fetchone()
        if request.param == 500 and not has_fts:
            pytest.skip(f"SQLite {sqlite3.sqlite_version} lacks FTS5/trigram; 499-document linear tests still run")
        if has_fts:
            rows = db.execute("SELECT COUNT(*) FROM sections_fts").fetchone()[0]
            sections = db.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
            assert rows == (sections if request.param == 500 else 0)
    return adapter


@pytest.mark.parametrize("query", ["?", "!", "a", "가", "a 가 ? !"])
def test_query_boundary_effective_empty(boundary_index: WikimapAdapter, query: str) -> None:
    assert boundary_index.search(query) == []
    assert boundary_index.last_terms == []
    assert boundary_index.last_weak is True
    assert boundary_index.last_partial is False


@pytest.mark.parametrize("query", [
    "공원에서", "공원", "literalneedle", '"exact phrase"',
    "title:policy", "path:park", "heading:walking", "tag:outdoors",
    "a title:policy", "공원에서 heading:walking",
])
def test_query_boundary_matches(boundary_index: WikimapAdapter, query: str) -> None:
    assert [result.file for result in boundary_index.search(query)] == ["park.md"]


def test_query_boundary_type_only(boundary_index: WikimapAdapter) -> None:
    results = boundary_index.search("type:md", n=1000)
    assert len(results) == len(list(boundary_index.snapshot_dir.glob("*.md")))
    assert {result.file for result in results} == {
        path.name for path in boundary_index.snapshot_dir.glob("*.md")
    }


@pytest.mark.parametrize("query", [
    "nonexistent", '"phrase exact"', "heading:공원에서", "공원에서 heading:absent",
    "공원에서 heading:restricted", "literalneedle heading:restricted",
])
def test_query_boundary_constraints(boundary_index: WikimapAdapter, query: str) -> None:
    assert boundary_index.search(query) == []


@pytest.mark.parametrize(("query", "error"), [
    ("", "empty query"), ("   ", "empty query"), ('""', "empty query"),
    ("type:unknown", "unknown type"), ("a type:unknown", "unknown type"),
])
def test_query_boundary_parse_errors(boundary_index: WikimapAdapter, query: str, error: str) -> None:
    with pytest.raises(WikimapAdapterError, match=error):
        boundary_index.search(query)


@pytest.mark.parametrize(("program", "error"), [
    ("import time; time.sleep(5)", "timed out"),
    ("print('success')", "failed to parse"),
    ('print(\'{"query":"?","results":[],"terms":[],"weak":true,"partial":false}\'); exit(2)', "exited 2"),
])
def test_query_boundary_subprocess_failures(tmp_path: Path, program: str, error: str) -> None:
    executable = tmp_path / "wikimap.py"
    executable.write_text(program, encoding="utf-8")
    adapter = WikimapAdapter(executable, tmp_path, timeout=1)
    with pytest.raises(WikimapAdapterError, match=error):
        adapter.search("?")
