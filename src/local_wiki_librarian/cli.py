"""Read-only Local Wiki Librarian CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Optional

from .audit import AuditContext, run_catalog_audit
from .authority import AuthorityManifest, compile_authority
from .catalog import Catalog, CatalogFinding, compile_catalog
from .connectome import extract_connectome
from .contracts import LibrarianQuery
from .lexical import WikimapAdapter
from .librarian import compile_answer_packet
from .manifest import load_authority_manifest, load_verified_generation
from .router import RouterConfig, WikiRouter, get_retriever_mode
from .vendor import bundled_vendor_root, bundled_wikimap_path, verify_vendor

DEFAULT_STATE_ROOT = Path.home() / ".librarian" / "wiki-retrieval"


def _find_wikimap_path() -> Optional[Path]:
    configured = os.environ.get("LIBRARIAN_WIKIMAP_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None
    try:
        vendor_root = bundled_vendor_root()
        verify_vendor(vendor_root)
        return bundled_wikimap_path()
    except (FileNotFoundError, OSError, RuntimeError):
        return None


def load_runtime(state_root: Path) -> tuple[Path, AuthorityManifest, Path]:
    state_root = Path(state_root)
    current, snapshot_dir, _ = load_verified_generation(state_root)
    generation = current["generation"]
    compiled_authority = compile_authority(snapshot_dir, generation)
    data = load_authority_manifest(state_root)
    if data is not None and data != compiled_authority.to_dict():
        raise RuntimeError("authority manifest differs from verified snapshot compilation")
    authority = compiled_authority
    wikimap_path = _find_wikimap_path()
    if not wikimap_path:
        raise FileNotFoundError("packaged wikimap runtime not found")
    return snapshot_dir, authority, wikimap_path


def load_operational_runtime(
    state_root: Path,
) -> tuple[Path, AuthorityManifest, Path]:
    """Load verified state and prove the exact index is queryable read-only."""
    snapshot_dir, authority, wikimap_path = load_runtime(state_root)
    if not (snapshot_dir / ".wikimap" / "index.db").is_file():
        raise FileNotFoundError("verified Wikimap index not found")
    WikimapAdapter(wikimap_path, snapshot_dir, timeout=2).search(
        "__local_wiki_librarian_health_probe__", n=1
    )
    return snapshot_dir, authority, wikimap_path


def run_librarian(
    query: str,
    state_root: Path,
    k: int = 10,
    graph_hops: int = 1,
    include_zero_score_graph: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    snapshot_dir, authority, wikimap_path = load_runtime(state_root)
    adapter = WikimapAdapter(wikimap_path, snapshot_dir)
    if not (snapshot_dir / ".wikimap" / "index.db").is_file():
        adapter.index()
    router = WikiRouter(adapter, authority, RouterConfig(
        k=k,
        graph_depth=graph_hops,
        include_zero_score_graph=include_zero_score_graph,
    ))
    results = router.search(query)
    entry_map = {entry.relative_path: entry for entry in authority.entries}
    payload = []
    for rank, result in enumerate(results, start=1):
        entry = entry_map.get(result.path)
        payload.append({
            "path": result.path,
            "canonical_path": result.path,
            "score": result.score,
            "tier": result.tier,
            "title": result.title,
            "source": result.source,
            "redirected_from": result.redirected_from,
            "engine": "librarian",
            "rank": rank,
            "domain": entry.domain if entry else None,
            "source_role": entry.source_role if entry else None,
            "status": entry.status if entry else None,
            "do_not_answer_as_current": entry.do_not_answer_as_current if entry else None,
            "last_verified": entry.last_verified if entry else None,
            "graph_distance": result.graph_distance,
        })
    return payload, list(router.last_warnings)


def _search(
    query: str,
    state_root: Path,
    k: int,
    graph_hops: int = 1,
    include_zero_score_graph: bool = False,
) -> dict[str, Any]:
    mode = get_retriever_mode().value
    try:
        results, warnings = run_librarian(
            query, state_root, k, graph_hops, include_zero_score_graph,
        )
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        return {"schema_version": 1, "status": "error", "mode": mode, "degraded": False,
                "warnings": [f"librarian unavailable: {type(exc).__name__}"], "results": []}
    state_counts = _snapshot_state_counts(state_root)
    state_warning = _snapshot_state_warning(state_counts)
    if state_warning:
        warnings.append(state_warning)
    return {"schema_version": 1, "status": "degraded" if warnings else "ok", "mode": mode,
            "degraded": bool(warnings), "warnings": warnings, "results": results}


def _snapshot_state_counts(state_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        current = json.loads((Path(state_root) / "current.json").read_text(encoding="utf-8"))
        for entry in current.get("files", []):
            state = str(entry.get("state", ""))
            counts[state] = counts.get(state, 0) + 1
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return counts


def _snapshot_state_warning(counts: dict[str, int]) -> Optional[str]:
    stale = counts.get("stale", 0)
    quarantined = counts.get("quarantined", 0)
    deleted = counts.get("deleted", 0)
    if stale or quarantined or deleted:
        return (
            "promoted snapshot is not fully fresh: "
            f"stale={stale}, quarantined={quarantined}, deleted={deleted}"
        )
    return None


def _health(state_root: Path) -> dict[str, Any]:
    mode = get_retriever_mode().value
    current = state_root / "current.json"
    state_available = current.is_file()
    try:
        authority_available = load_authority_manifest(state_root) is not None
    except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError):
        authority_available = False
    librarian_available = False
    warning: Optional[str] = None
    try:
        load_operational_runtime(state_root)
        librarian_available = True
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        warning = f"Librarian runtime unavailable: {type(exc).__name__}"
    state_counts = _snapshot_state_counts(state_root)
    state_warning = _snapshot_state_warning(state_counts) if librarian_available else None
    warnings = [item for item in (warning, state_warning) if item]
    status = "error" if not librarian_available else ("degraded" if state_warning else "ok")
    return {"schema_version": 1, "status": status, "mode": mode,
            "degraded": bool(state_warning), "state_root": str(state_root),
            "state_available": state_available, "authority_available": authority_available,
            "librarian_available": librarian_available, "wikimap_available": librarian_available,
            "snapshot_state_counts": state_counts,
            "operational_backends": ["librarian"] if librarian_available else [],
            "warnings": warnings}


def _packet(command: str, args: argparse.Namespace) -> dict[str, Any]:
    search = _search(
        args.query,
        args.state_root,
        args.num,
        args.graph_hops,
        args.include_zero_score_graph,
    )
    if search["status"] == "error":
        return {
            "schema_version": 1,
            "status": "error",
            "degraded": False,
            "warnings": search["warnings"],
            "error": "retrieval runtime unavailable",
        }
    try:
        snapshot_dir, _, _ = load_runtime(args.state_root)
        catalog = compile_catalog(snapshot_dir)
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, RuntimeError):
        catalog = Catalog(source_path="catalog.md")
        catalog.findings.append(CatalogFinding(
            code="MALFORMED_REGISTRY",
            message="catalog unavailable from verified snapshot",
            path=catalog.source_path,
        ))
    intent = "locate" if command == "locate" else ("audit" if command == "trace" else args.intent)
    packet = compile_answer_packet(
        LibrarianQuery(
            query=args.query,
            intent=intent,
            time_scope=args.time_scope,
            requested_domain=args.domain,
            max_graph_hops=args.graph_hops,
        ),
        catalog,
        search["results"],
        degraded=bool(search["degraded"]),
        warnings=search["warnings"] + [finding.message for finding in catalog.findings],
    )
    return packet.to_dict()


def _audit(state_root: Path) -> dict[str, Any]:
    try:
        snapshot_dir, authority, _ = load_operational_runtime(state_root)
        catalog = compile_catalog(snapshot_dir)
        connectome = extract_connectome(snapshot_dir)
        report = run_catalog_audit(AuditContext(
            catalog=catalog,
            authority=authority,
            connectome=connectome,
            index_degraded=not (snapshot_dir / ".wikimap" / "index.db").is_file(),
        ))
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        report = run_catalog_audit(AuditContext(
            catalog=Catalog(source_path="catalog.md"),
            authority=AuthorityManifest(generation="unavailable"),
            index_degraded=True,
        ))
        data = report.to_dict()
        data.update({
            "status": "degraded",
            "degraded": True,
            "warnings": [f"audit degraded: {type(exc).__name__}"],
        })
        return data
    data = report.to_dict()
    data.update({
        "status": "ok" if data.get("passed") else "degraded",
        "degraded": not bool(data.get("passed")),
        "warnings": [],
    })
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Local Wiki Librarian")
    parser.add_argument("command", choices=["health", "search", "ask", "locate", "trace", "audit"])
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--state-root", type=Path, default=Path(os.environ.get("LIBRARIAN_STATE", str(DEFAULT_STATE_ROOT))).expanduser())
    parser.add_argument("-n", "--num", type=int, default=10)
    parser.add_argument("--intent", default="explain")
    parser.add_argument("--time-scope", choices=["historical", "saved_knowledge", "current"], default="saved_knowledge")
    parser.add_argument("--domain")
    parser.add_argument("--graph-hops", type=int, default=1)
    parser.add_argument("--include-zero-score-graph", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"search", "ask", "locate", "trace"} and not args.query.strip():
        print(json.dumps({"schema_version": 1, "status": "error", "error": "query required"}))
        return 2
    if args.command == "health":
        data = _health(args.state_root)
    elif args.command == "search":
        data = _search(
            args.query,
            args.state_root,
            args.num,
            args.graph_hops,
            args.include_zero_score_graph,
        )
    elif args.command in {"ask", "locate", "trace"}:
        try:
            data = _packet(args.command, args)
        except (ValueError, TypeError, OSError, KeyError, json.JSONDecodeError) as exc:
            data = {
                "schema_version": 1,
                "status": "error",
                "error": "bounded librarian failure",
                "error_type": type(exc).__name__,
            }
    else:
        data = _audit(args.state_root)
    print(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    if data.get("status") == "error":
        return 2
    if args.command == "audit" and data.get("passed") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
