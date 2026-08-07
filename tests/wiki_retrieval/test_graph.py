"""Tests for the read-only wikimap graph adapter."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.lexical import WikimapAdapter, WikimapAdapterError  # noqa: E402
from local_wiki_librarian.graph import GraphAdapter, PathResult  # noqa: E402

WIKIMAP_PATH = ROOT / "src" / "local_wiki_librarian" / "_vendor" / "wikimap" / "wikimap.py"


def _make_linked_wiki(snapshot_dir: Path) -> None:
    """Create a wiki fixture with interlinked documents."""
    (snapshot_dir / "knowledge/policies").mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "knowledge/policies/policy.md").write_text(
        "---\nwiki_schema: librarian-v1\nauthority: high\nstatus: active\n---\n\n"
        "# Policy\n\n**관련**: [[knowledge/policies/guide]], [[domains/wiki-guide]]\n\n"
        "Content about policy.\n",
        encoding="utf-8",
    )
    (snapshot_dir / "knowledge/policies/guide.md").write_text(
        "---\nwiki_schema: librarian-v1\nauthority: high\nstatus: active\n---\n\n"
        "# Guide\n\n**관련**: [[knowledge/policies/policy]]\n\n"
        "Content about guide.\n",
        encoding="utf-8",
    )
    (snapshot_dir / "domains").mkdir(exist_ok=True)
    (snapshot_dir / "domains/wiki-guide.md").write_text(
        "---\ntitle: Wiki Guide\n---\n\n# Wiki Guide\n\n"
        "**관련**: [[knowledge/policies/policy]]\n\nGeneral wiki operations.\n",
        encoding="utf-8",
    )


class TestGraphLinks:
    def test_links_returns_outlinks(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_linked_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        graph = GraphAdapter(adapter)
        links = graph.links("knowledge/policies/policy.md")
        assert len(links["outlinks"]) > 0

    def test_links_returns_backlinks(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_linked_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        graph = GraphAdapter(adapter)
        links = graph.links("knowledge/policies/policy.md")
        # guide.md references policy.md, so backlinks should exist
        assert len(links["backlinks"]) > 0

    def test_links_nonexistent_doc(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_linked_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        graph = GraphAdapter(adapter)
        links = graph.links("nonexistent.md")
        assert links["outlinks"] == []
        assert links["backlinks"] == []

    def test_links_propagates_non_not_found_adapter_error(self, tmp_path: Path, monkeypatch) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        graph = GraphAdapter(adapter)

        def fail(*_args, **_kwargs):
            raise WikimapAdapterError("wikimap links exited 2: corrupt index")

        monkeypatch.setattr(adapter, "_run", fail)
        with pytest.raises(WikimapAdapterError, match="corrupt index"):
            graph.links("broken.md")


class TestGraphPath:
    def test_path_finds_connection(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_linked_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        graph = GraphAdapter(adapter)
        # policy links to guide, guide links to policy
        result = graph.path("knowledge/policies/policy.md", "knowledge/policies/guide.md")
        assert isinstance(result, PathResult)
        assert result.src == "knowledge/policies/policy.md"
        assert result.dst == "knowledge/policies/guide.md"

    def test_path_no_connection(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_linked_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        graph = GraphAdapter(adapter)
        result = graph.path("knowledge/policies/policy.md", "nonexistent.md")
        assert result.found is False
