"""Librarian/Wikimap-only authority-aware retrieval router.

Pipeline order:
1. Authority lexical search via Wikimap over the promoted snapshot
2. Bounded graph expansion
3. Current/authority filtering and redirect resolution
4. Exact snapshot readback
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .authority import AuthorityManifest
from .lexical import WikimapAdapter
from .graph import GraphAdapter
from .paths import UnsafePathError, resolve_within

__all__ = [
    "RetrieverMode",
    "RouterConfig",
    "RouterResult",
    "WikiRouter",
    "get_retriever_mode",
]


class RetrieverMode(enum.Enum):
    """The sole supported local-Wiki retrieval engine."""

    LIBRARIAN = "librarian"


def get_retriever_mode() -> RetrieverMode:
    """Return the package's single local retrieval mode."""
    return RetrieverMode.LIBRARIAN


@dataclass(frozen=True)
class RouterConfig:
    """Configuration for the hybrid router."""

    k: int = 10
    graph_expand: bool = True
    graph_depth: int = 1
    authority_filter: bool = True
    demote_raw: bool = True
    demote_index: bool = True
    demote_history: bool = True
    follow_redirects: bool = True
    include_zero_score_graph: bool = False

    def __post_init__(self) -> None:
        if self.graph_depth < 0 or self.graph_depth > 2:
            raise ValueError("graph_depth must be between 0 and 2")


@dataclass(frozen=True)
class RouterResult:
    """One result from the hybrid router."""

    path: str
    score: float
    tier: str
    title: str = ""
    snippet: str = ""
    source: str = "wikimap"  # wikimap | graph | redirect
    redirected_from: str | None = None
    graph_distance: int | None = None


class WikiRouter:
    """Authority-aware hybrid retrieval router.

    Requires a pre-built snapshot + authority manifest + wikimap index.
    """

    def __init__(
        self,
        adapter: WikimapAdapter,
        authority_manifest: AuthorityManifest,
        config: RouterConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.graph = GraphAdapter(adapter)
        self.authority = authority_manifest
        self.config = config or RouterConfig()
        self.last_warnings: list[str] = []

        # Build authority tier lookup
        self._tier_map: dict[str, str] = {}
        self._redirect_map: dict[str, str] = {}
        self._do_not_answer_map: dict[str, bool] = {}
        for entry in authority_manifest.entries:
            self._tier_map[entry.relative_path] = entry.tier
            self._do_not_answer_map[entry.relative_path] = entry.do_not_answer_as_current is True
            if entry.tier == "redirect" and entry.canonical_path:
                self._redirect_map[entry.relative_path] = entry.canonical_path

    def _demote_score(self, path: str, base_score: float) -> float:
        """Apply authority-based score demotion."""
        tier = self._tier_map.get(path, "unknown")
        if self.config.demote_raw and tier == "raw":
            return base_score * 0.1
        if self.config.demote_index and tier == "index":
            return base_score * 0.3
        if self.config.demote_history and tier == "history":
            return base_score * 0.05
        return base_score

    def _resolve_redirect(self, path: str) -> tuple[str, str | None]:
        """Follow redirect to canonical path. Returns (resolved_path, original_if_redirected)."""
        if not self.config.follow_redirects:
            return path, None
        if path in self._redirect_map:
            canonical = self._redirect_map[path]
            # Normalize: ensure .md extension
            if not canonical.endswith(".md"):
                canonical = canonical + ".md"
            return canonical, path
        return path, None

    def search(self, query: str) -> list[RouterResult]:
        """Run the full hybrid retrieval pipeline.

        Args:
            query: Search query string.

        Returns:
            Ranked list of RouterResult, authority-filtered and redirect-resolved.
        """
        self.last_warnings = []

        # 1. Lexical search via wikimap
        raw_results = self.adapter.search(
            query,
            n=max(100, self.config.k * 20),
        )
        dead_terms = [
            str(item.get("term", ""))
            for item in getattr(self.adapter, "last_terms", [])
            if item.get("df") == 0
        ]
        if getattr(self.adapter, "last_weak", False) and dead_terms:
            self.last_warnings.append("weak lexical query contains dead vocabulary; returning no answer")
            return []

        results: list[RouterResult] = []
        seen_paths: set[str] = set()

        for r in raw_results:
            resolved_path, redirected_from = self._resolve_redirect(r.file)
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)

            tier = self._tier_map.get(resolved_path, "unknown")
            if self.config.authority_filter and (
                tier not in {"current", "authority"}
                or self._do_not_answer_map.get(resolved_path, False)
            ):
                continue
            adjusted_score = self._demote_score(resolved_path, r.score)

            results.append(RouterResult(
                path=resolved_path,
                score=adjusted_score,
                tier=tier,
                title=r.title,
                snippet=r.snippet,
                source="redirect" if redirected_from else "wikimap",
                redirected_from=redirected_from,
            ))


        # 2. Graph expansion (optional, bounded to at most two hops)
        if self.config.graph_expand and self.config.graph_depth > 0 and results:
            frontier = [r.path for r in results[:3]]
            for distance in range(1, self.config.graph_depth + 1):
                next_frontier: list[str] = []
                for top_path in frontier:
                    try:
                        links = self.graph.links(top_path)
                    except Exception as exc:
                        self.last_warnings.append(
                            f"graph expansion failed for {top_path}: {type(exc).__name__}"
                        )
                        continue
                    for link in links["outlinks"][:3]:
                        link_path = link.target
                        if not link_path.endswith(".md"):
                            link_path = link_path + ".md"
                        resolved, redirected = self._resolve_redirect(link_path)
                        if resolved in seen_paths:
                            continue
                        seen_paths.add(resolved)
                        next_frontier.append(resolved)
                        tier = self._tier_map.get(resolved, "unknown")
                        if self.config.authority_filter and (
                            tier not in {"current", "authority"}
                            or self._do_not_answer_map.get(resolved, False)
                        ):
                            continue
                        results.append(RouterResult(
                            path=resolved,
                            score=0.0,
                            tier=tier,
                            title=link.title,
                            source="graph",
                            redirected_from=redirected,
                            graph_distance=distance,
                        ))
                frontier = next_frontier
                if not frontier:
                    break

        # 3. Hide score-zero graph-only results at the presentation boundary.
        # Graph nodes remain in ``seen_paths`` and the traversal frontier, so
        # the default precision policy does not change bounded depth-2 graph
        # reachability. Lexical/redirect score-zero results remain visible.
        if not self.config.include_zero_score_graph:
            results = [
                result for result in results
                if not (result.source == "graph" and result.score == 0.0)
            ]

        # 4. Sort by adjusted score (authority-boosted/demoted)
        results.sort(key=lambda r: r.score, reverse=True)

        # 5. Truncate to k
        return results[: self.config.k]

    def readback(self, path: str, max_lines: int = 120) -> str | None:
        """Read file content from the snapshot for exact readback.

        Args:
            path: Relative path within the snapshot.
            max_lines: Maximum lines to return.

        Returns:
            File content as string, or None if not found.
        """
        try:
            file_path = resolve_within(self.adapter.snapshot_dir, path)
        except UnsafePathError:
            return None
        if not file_path.is_file():
            return None
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[:max_lines])
        except OSError:
            return None
