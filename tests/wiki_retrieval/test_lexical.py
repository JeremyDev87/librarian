"""Tests for the read-only wikimap lexical adapter."""
from __future__ import annotations

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
