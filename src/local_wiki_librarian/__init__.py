"""Librarian-owned fail-closed wiki retrieval foundation."""

from .config import RetrievalConfig
from .paths import UnsafePathError, resolve_within, validate_disjoint_roots
from .snapshot import SnapshotError, SnapshotResult, build_snapshot
from .frontmatter import FrontmatterError, ParsedFrontmatter, parse_frontmatter, parse_frontmatter_bytes
from .authority import AuthorityEntry, AuthorityManifest, compile_authority
from .manifest import build_authority_manifest, load_authority_manifest
from .lexical import WikimapAdapter, WikimapAdapterError, SearchResult, ALLOWED_COMMANDS, BLOCKED_COMMANDS
from .graph import GraphAdapter, LinkInfo, PathResult
from .scorer import EvalReport, QueryResult, aggregate_reports, format_report, score_query
from .router import RetrieverMode, RouterConfig, RouterResult, WikiRouter, get_retriever_mode

__all__ = [
    "RetrievalConfig",
    "UnsafePathError",
    "SnapshotError",
    "SnapshotResult",
    "build_snapshot",
    "FrontmatterError",
    "ParsedFrontmatter",
    "parse_frontmatter",
    "parse_frontmatter_bytes",
    "AuthorityEntry",
    "AuthorityManifest",
    "compile_authority",
    "build_authority_manifest",
    "load_authority_manifest",
    "WikimapAdapter",
    "WikimapAdapterError",
    "SearchResult",
    "ALLOWED_COMMANDS",
    "BLOCKED_COMMANDS",
    "GraphAdapter",
    "LinkInfo",
    "PathResult",
    "EvalReport",
    "QueryResult",
    "aggregate_reports",
    "format_report",
    "score_query",
    "RetrieverMode",
    "RouterConfig",
    "RouterResult",
    "WikiRouter",
    "get_retriever_mode",
    "resolve_within",
    "validate_disjoint_roots",
]
