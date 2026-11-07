"""Tests for the hybrid retrieval router."""
from __future__ import annotations

import sys

import pytest
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
from local_wiki_librarian.lexical import SearchResult, WikimapAdapter, WikimapAdapterError  # noqa: E402
from local_wiki_librarian.scorer import aggregate_reports, score_query  # noqa: E402
from local_wiki_librarian.soak import validate_query_result  # noqa: E402

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
    def test_graph_only_zero_score_is_hidden_by_default_and_opt_in_restores_it(
        self, tmp_path: Path,
    ) -> None:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        _make_mini_wiki(snapshot)
        adapter = WikimapAdapter(WIKIMAP_PATH, snapshot)
        adapter.index()
        authority = _make_authority([
            AuthorityEntry(
                relative_path="knowledge/policies/policy.md",
                tier="current",
                has_frontmatter=True,
            ),
            AuthorityEntry(
                relative_path="knowledge/policies/guide.md",
                tier="current",
                has_frontmatter=True,
            ),
        ])

        default_results = WikiRouter(
            adapter, authority, RouterConfig(graph_depth=1),
        ).search("split index policy")
        legacy_results = WikiRouter(
            adapter,
            authority,
            RouterConfig(graph_depth=1, include_zero_score_graph=True),
        ).search("split index policy")

        assert all(not (item.source == "graph" and item.score == 0.0) for item in default_results)
        assert any(item.source == "graph" and item.score == 0.0 for item in legacy_results)

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


@pytest.mark.parametrize(
    'corpus_size,eligible_count,returned_count,duplicates,filter_enabled,expected_calls',
    [
        (450, 5, 450, False, True, [100]),
        (450, 0, 99, False, True, [100]),
        (450, 0, 199, False, True, [100, 200]),
        (450, 0, 450, False, True, [100, 200, 400, 450]),
        (150, 0, 150, True, True, [100, 150]),
        (50, 0, 50, False, True, [100]),
        (450, 0, 450, False, False, [100]),
    ],
)
def test_candidate_refill_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    corpus_size: int, eligible_count: int, returned_count: int,
    duplicates: bool, filter_enabled: bool, expected_calls: list[int],
) -> None:
    entries = [
        AuthorityEntry(relative_path=f'{index}.md', tier='current' if index < eligible_count else 'raw')
        for index in range(corpus_size)
    ]
    hits = [SearchResult(entry.relative_path, 1, 'Policy', 10.0) for entry in entries[:returned_count]]
    adapter = WikimapAdapter(WIKIMAP_PATH, tmp_path)
    calls = []

    def search(query: str, n: int = 10) -> list[SearchResult]:
        calls.append(n)
        assert len(calls) <= 4, 'candidate refill failed to terminate'
        return hits[:n]

    monkeypatch.setattr(adapter, 'search', search)
    router = WikiRouter(
        adapter, _make_authority(entries * (2 if duplicates else 1)),
        RouterConfig(k=5, graph_expand=False, authority_filter=filter_enabled),
    )
    results = router.search('policy')

    assert calls == expected_calls
    assert [item.path for item in results] == [
        f'{index}.md' for index in range(eligible_count if filter_enabled else 5)
    ]
    assert all(item.score == (10.0 if filter_enabled else 1.0) for item in results)


def test_candidate_refill_redirect_dedup_latest_prefix_and_graph_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        AuthorityEntry(relative_path=f'alias-{index}.md', tier='redirect', canonical_path='current')
        for index in range(100)
    ] + [AuthorityEntry(relative_path='current.md', tier='current')]
    entries += [AuthorityEntry(relative_path=f'next-{index}.md', tier='authority') for index in range(4)]
    entries += [AuthorityEntry(relative_path=f'raw-{index}.md', tier='raw') for index in range(300)]
    adapter = WikimapAdapter(WIKIMAP_PATH, tmp_path)
    calls = []

    def search(query: str, n: int = 10) -> list[SearchResult]:
        calls.append(n)
        hits = [SearchResult(entry.relative_path, 1, str(n), float(n)) for entry in entries]
        return hits[:n]

    monkeypatch.setattr(adapter, 'search', search)
    router = WikiRouter(adapter, _make_authority(entries), RouterConfig(k=5))
    graph_calls = []

    def links(path: str) -> dict[str, list]:
        graph_calls.append((path, list(calls)))
        return {'outlinks': [], 'backlinks': []}

    monkeypatch.setattr(router.graph, 'links', links)
    results = router.search('policy')

    assert calls == [100, 200]
    assert [item.path for item in results] == ['current.md'] + [f'next-{index}.md' for index in range(4)]
    assert all(item.score == 200.0 and item.title == '200' for item in results)
    assert results[0].redirected_from == 'alias-0.md'
    assert graph_calls == [(path, [100, 200]) for path in ['current.md', 'next-0.md', 'next-1.md']]


@pytest.mark.parametrize('outcome', ['weak_dead', 'adapter_error'])
def test_candidate_refill_preserves_policy_on_later_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    entries = [AuthorityEntry(relative_path=f'{index}.md', tier='raw') for index in range(100)]
    entries.append(AuthorityEntry(relative_path='current.md', tier='current'))
    adapter = WikimapAdapter(WIKIMAP_PATH, tmp_path)
    calls = []

    def search(query: str, n: int = 10) -> list[SearchResult]:
        calls.append(n)
        if len(calls) == 2:
            if outcome == 'adapter_error':
                raise WikimapAdapterError('refill failed')
            adapter.last_weak = True
            adapter.last_terms = [{'term': 'absent', 'df': 0}]
        return [SearchResult(entry.relative_path, 1, 'Policy', 10.0) for entry in entries[:n]]

    monkeypatch.setattr(adapter, 'search', search)
    router = WikiRouter(adapter, _make_authority(entries), RouterConfig(k=5, graph_expand=False))
    if outcome == 'adapter_error':
        with pytest.raises(WikimapAdapterError, match='refill failed'):
            router.search('policy absent')
    else:
        assert router.search('policy absent') == []
        assert len(router.last_warnings) == 1
    assert calls == [100, 101]


@pytest.mark.parametrize('raw_count', [199, 200])
@pytest.mark.parametrize('k', [10, 11])
def test_candidate_refill_actual_vendor_crowding(tmp_path: Path, raw_count: int, k: int) -> None:
    (tmp_path / 'raw').mkdir()
    for index in range(raw_count):
        (tmp_path / f'raw/backup-{index}.md').write_text('# Backup\nbackup backup backup\n', encoding='utf-8')
    (tmp_path / 'current.md').write_text('---\nauthority: high\nstatus: active\n---\n# Operations\nbackup\n', encoding='utf-8')
    adapter = WikimapAdapter(WIKIMAP_PATH, tmp_path)
    adapter.index()
    authority = compile_authority(tmp_path, 'test')
    router = WikiRouter(adapter, authority, RouterConfig(k=k, graph_expand=False))

    results = router.search('backup')

    assert [item.path for item in results] == ['current.md']
    assert all(item.tier == 'current' for item in results)


def test_candidate_refill_synthetic_contract_table(tmp_path: Path) -> None:
    (tmp_path / 'raw').mkdir()
    for index in range(200):
        (tmp_path / f'raw/backup-{index}.md').write_text('# Backup\nbackup backup backup\n', encoding='utf-8')
    (tmp_path / 'current.md').write_text('---\nauthority: high\nstatus: active\n---\n# Operations\nbackup\n', encoding='utf-8')
    (tmp_path / 'park.md').write_text('---\nauthority: high\nstatus: active\n---\n# Park\n공원\n', encoding='utf-8')
    adapter = WikimapAdapter(WIKIMAP_PATH, tmp_path)
    adapter.index()
    authority = compile_authority(tmp_path, 'test')
    router = WikiRouter(adapter, authority, RouterConfig(k=5, graph_expand=False))
    cases = [
        {'query': 'Operations', 'expected_paths': ['current.md']},
        {'query': '공원에서', 'expected_paths': ['park.md']},
        {'query': 'title:Operations', 'expected_paths': ['current.md']},
        {'query': 'backup', 'expected_paths': ['current.md']},
        {'query': '공원 nonexistent', 'expected_no_answer': True},
        {'query': '공원 산책을 알려주세요', 'expected_no_answer': True},
        {'query': 'unknown-vocabulary', 'expected_no_answer': True},
        {'query': '?', 'expected_no_answer': True},
    ]
    reports = []
    for case in cases:
        paths = [item.path for item in router.search(case['query'])]
        validate_query_result(case, paths)
        reports.append(score_query(
            case['query'], case.get('expected_paths', []), paths,
            [entry.relative_path for entry in authority.entries if entry.tier == 'raw'],
            k=5, expected_no_answer=case.get('expected_no_answer', False),
        ))
    report = aggregate_reports('synthetic-router', reports)

    assert report.query_count == len(cases)
    assert report.avg_recall_at_k == report.avg_mrr == report.top1_accuracy == 1.0
    assert report.authority_false_positive_count == 0


@pytest.mark.parametrize('query', ['', 'type:unknown'])
def test_candidate_refill_actual_vendor_errors(tmp_path: Path, query: str) -> None:
    _make_mini_wiki(tmp_path)
    adapter = WikimapAdapter(WIKIMAP_PATH, tmp_path)
    adapter.index()
    router = WikiRouter(adapter, compile_authority(tmp_path, 'test'))

    with pytest.raises(WikimapAdapterError):
        router.search(query)
