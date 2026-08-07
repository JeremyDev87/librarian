"""Tests for the hybrid retrieval router."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.router import (  # noqa: E402
    RetrieverMode,
    RouterConfig,

    WikiRouter,
    get_retriever_mode,
)
from local_wiki_librarian.authority import (  # noqa: E402
    AuthorityEntry,
    AuthorityManifest,
    compile_authority,
)
from local_wiki_librarian.lexical import WikimapAdapter  # noqa: E402

WIKIMAP_PATH = ROOT / "src" / "local_wiki_librarian" / "_vendor" / "wikimap" / "wikimap.py"


def _make_mini_wiki(snapshot_dir: Path) -> None:
    """Create a wiki fixture with authority tiers."""
    (snapshot_dir / "knowledge/policies").mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "knowledge/policies/policy.md").write_text(
        "---\nwiki_schema: librarian-v1\nauthority: high\nstatus: active\n---\n\n"
        "# Local Index Policy\n\nContent about split index policy.\n"
        "**관련**: [[knowledge/policies/guide]]\n",
        encoding="utf-8",
    )
    (snapshot_dir / "knowledge/policies/guide.md").write_text(
        "---\nwiki_schema: librarian-v1\nauthority: high\nstatus: active\n---\n\n"
        "# Guide\n\n**관련**: [[knowledge/policies/policy]]\n\nGuide content.\n",
        encoding="utf-8",
    )
    (snapshot_dir / "raw").mkdir(exist_ok=True)
    (snapshot_dir / "raw/source-map.md").write_text(
        "---\nwiki_schema: librarian-v1\nauthority: evidence\n---\n\n"
        "# Source map about policy\n\nEvidence content.\n",
        encoding="utf-8",
    )


def _make_authority(entries: list[AuthorityEntry]) -> AuthorityManifest:
    tier_counts: dict[str, int] = {}
    redirect_map: dict[str, str] = {}
    for e in entries:
        tier_counts[e.tier] = tier_counts.get(e.tier, 0) + 1
        if e.tier == "redirect" and e.canonical_path:
            redirect_map[e.relative_path] = e.canonical_path
    return AuthorityManifest(
        generation="test",
        entries=entries,
        tier_counts=tier_counts,
        redirect_map=redirect_map,
        unresolved_redirects=[],
    )


class TestRetrieverMode:
    def test_single_mode_is_librarian(self) -> None:
        assert get_retriever_mode() == RetrieverMode.LIBRARIAN

    def test_mode_enum_has_no_hidden_backends(self) -> None:
        assert list(RetrieverMode) == [RetrieverMode.LIBRARIAN]



class TestRouterSearch:
    def test_router_has_no_metadata_cue_injection_surface(self) -> None:
        assert not hasattr(WikiRouter, "_intent_authority_entries")

    def test_malformed_exclusion_metadata_cannot_surface_as_current(
        self, tmp_path: Path,
    ) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        page = snapshot / "unsafe.md"
        page.write_text(
            "---\nauthority: high\nstatus: active\n"
            "do_not_answer_as_current: 'true'\n---\n"
            "# Unsafe\nmalformed exclusion sentinel\n",
            encoding="utf-8",
        )
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = compile_authority(snapshot, "test")

        assert WikiRouter(adapter, authority).search("malformed exclusion sentinel") == []

    def test_weak_query_with_dead_vocabulary_returns_no_answer(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = _make_authority([
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
        ])
        assert WikiRouter(adapter, authority).search("목성 nonexistent-wiki-term") == []

    def test_dead_vocabulary_does_not_inject_metadata_owner(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = _make_authority([
            AuthorityEntry(
                relative_path="knowledge/policies/policy.md", tier="current",
                has_frontmatter=True, source_role="custom_policy", status="active",
            ),
        ])
        assert WikiRouter(adapter, authority).search("unindexed operational phrase") == []

    def test_dead_schedule_fiction_still_returns_no_answer(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        (snapshot / "knowledge/schedule.md").write_text("# Schedule\ncalendar", encoding="utf-8")
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = _make_authority([
            AuthorityEntry(
                relative_path="knowledge/schedule.md", tier="current", has_frontmatter=True,
                source_role="schedule", status="active",
            ),
        ])
        assert WikiRouter(adapter, authority).search("2099년 화성 기지 점심 일정") == []

    def test_exact_lexical_terms_rank_matching_page(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        page = snapshot / "domains/llm-wiki/librarian-artifact-storage-boundary.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\nstatus: active\n---\n# Librarian artifact storage boundary", encoding="utf-8")
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = _make_authority([
            AuthorityEntry(relative_path=page.relative_to(snapshot).as_posix(), tier="current", status="active"),
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", status="active"),
        ])
        results = WikiRouter(adapter, authority).search("artifact storage boundary")
        assert results[0].path == "domains/llm-wiki/librarian-artifact-storage-boundary.md"

    def test_typed_current_owner_is_injected_for_conversational_intent(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        (snapshot / "knowledge/current-tasks.md").write_text(
            "---\nstatus: active\n---\n# Current tasks\ntask list\n", encoding="utf-8"
        )
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = _make_authority([
            AuthorityEntry(
                relative_path="knowledge/current-tasks.md", tier="current", has_frontmatter=True,
                source_role="task_list", status="active",
            ),
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
        ])
        assert WikiRouter(adapter, authority).search("task")[0].path == "knowledge/current-tasks.md"

    def test_returns_results(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()

        authority = _make_authority([
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
            AuthorityEntry(relative_path="knowledge/policies/guide.md", tier="current", has_frontmatter=True),
            AuthorityEntry(relative_path="raw/source-map.md", tier="raw", has_frontmatter=True),
        ])

        router = WikiRouter(adapter, authority)
        results = router.search("policy")
        assert len(results) > 0

    def test_raw_demoted(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()

        authority = _make_authority([
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
            AuthorityEntry(relative_path="raw/source-map.md", tier="raw", has_frontmatter=True),
        ])

        router = WikiRouter(adapter, authority)
        results = router.search("policy")
        # If raw appears, it should have a demoted score
        raw_results = [r for r in results if r.tier == "raw"]
        current_results = [r for r in results if r.tier == "current"]
        if raw_results and current_results:
            assert raw_results[0].score < current_results[0].score

    def test_results_include_tier(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()

        authority = _make_authority([
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
        ])

        router = WikiRouter(adapter, authority)
        results = router.search("policy")
        for r in results:
            assert r.tier in ("current", "authority", "raw", "index", "history", "redirect", "unknown")

    def test_readback_returns_content(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)

        authority = _make_authority([])
        router = WikiRouter(adapter, authority)
        content = router.readback("knowledge/policies/policy.md")
        assert content is not None
        assert "policy" in content.lower()

    def test_readback_nonexistent_returns_none(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        authority = _make_authority([])
        router = WikiRouter(adapter, authority)
        assert router.readback("nonexistent.md") is None


class TestRouterRedirect:
    def test_redirect_resolved(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()

        authority = _make_authority([
            AuthorityEntry(
                relative_path="knowledge/policies/old-policy.md",
                tier="redirect",
                canonical_path="knowledge/policies/policy.md",
                has_frontmatter=True,
            ),
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
        ])

        router = WikiRouter(adapter, authority)
        results = router.search("policy")
        # Redirect target should appear, redirect source should be resolved
        paths = [r.path for r in results]
        assert "knowledge/policies/policy.md" in paths


class TestRouterConfig:
    def test_graph_expand_disabled(self, tmp_path: Path) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()

        authority = _make_authority([
            AuthorityEntry(relative_path="knowledge/policies/policy.md", tier="current", has_frontmatter=True),
        ])

        config = RouterConfig(graph_expand=False, k=5)
        router = WikiRouter(adapter, authority, config)
        results = router.search("policy")
        # Without graph expansion, all results should be from wikimap
        assert all(r.source == "wikimap" for r in results)
