"""Deterministic, read-only Connectome v1 typed-edge extraction."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .paths import resolve_within

ALLOWED_RELATIONS = frozenset({
    "governed_by", "routes_to", "supports", "derives_from",
    "part_of", "supersedes", "conflicts_with",
})
FORBIDDEN_AUTHORITY_RELATIONS = frozenset({
    "authoritative_for", "canonical_for", "confers_authority", "current_authority",
})
_EDGE_RE = re.compile(
    r"^\s*[-*]\s+\*\*([a-z_]+)\*\*:\s*\[\[([^\]]+)\]\]\s*$"
)


class ConnectomeError(RuntimeError):
    pass


@dataclass(frozen=True)
class TypedEdge:
    source: str
    relation: str
    target: str
    line: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ConnectomeFinding:
    code: str
    message: str
    source: str
    line: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Connectome:
    edges: list[TypedEdge] = field(default_factory=list)
    findings: list[ConnectomeFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "edges": [edge.to_dict() for edge in self.edges],
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _normalize_target(raw: str) -> str:
    target = raw.split("|", 1)[0].strip().replace("\\", "/")
    page, marker, heading = target.partition("#")
    logical = PurePosixPath(page)
    if page.startswith("/") or ".." in logical.parts:
        return ""
    normalized = logical.as_posix().strip("/")
    if not normalized.endswith(".md"):
        normalized += ".md"
    return normalized + (marker + heading if marker else "")


def extract_connectome(snapshot_root: Path) -> Connectome:
    """Extract only typed bullets under an exact level-2 ``## 연결`` section."""
    root = Path(snapshot_root)
    graph = Connectome()
    for source_path in sorted(root.rglob("*.md")):
        if source_path.is_symlink():
            continue
        source = str(source_path.relative_to(root))
        try:
            lines = source_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            graph.findings.append(ConnectomeFinding(
                code="CONNECTOME_READ_ERROR", message=type(exc).__name__, source=source
            ))
            continue
        in_section = False
        for line_number, line in enumerate(lines, start=1):
            if line == "## 연결":
                in_section = True
                continue
            if in_section and line.startswith("#"):
                in_section = False
            if not in_section or not line.lstrip().startswith(("-", "*")):
                continue
            match = _EDGE_RE.match(line)
            if not match:
                if "**" in line and "[[" in line:
                    graph.findings.append(ConnectomeFinding(
                        code="MALFORMED_TYPED_EDGE",
                        message="typed edge must contain one relation and one target",
                        source=source,
                        line=line_number,
                    ))
                continue
            relation, raw_target = match.groups()
            if relation in FORBIDDEN_AUTHORITY_RELATIONS:
                graph.findings.append(ConnectomeFinding(
                    code="FORBIDDEN_RELATION",
                    message=f"{relation} could confer authority",
                    source=source,
                    line=line_number,
                ))
                continue
            if relation not in ALLOWED_RELATIONS:
                graph.findings.append(ConnectomeFinding(
                    code="UNSUPPORTED_RELATION",
                    message=relation,
                    source=source,
                    line=line_number,
                ))
                continue
            target = _normalize_target(raw_target)
            if not target:
                graph.findings.append(ConnectomeFinding(
                    code="UNSAFE_TYPED_EDGE",
                    message="typed edge target escapes the snapshot root",
                    source=source,
                    line=line_number,
                ))
                continue
            target_page = target.split("#", 1)[0]
            if not resolve_within(root, target_page).is_file():
                graph.findings.append(ConnectomeFinding(
                    code="BROKEN_TYPED_EDGE",
                    message=f"target not found: {target_page}",
                    source=source,
                    line=line_number,
                ))
            graph.edges.append(TypedEdge(
                source=source, relation=relation, target=target, line=line_number
            ))
    return graph


def expand_connectome(seed: str, graph: Connectome, max_hops: int = 1) -> list[str]:
    """Breadth-first expansion with a hard two-hop limit."""
    if max_hops < 0 or max_hops > 2:
        raise ConnectomeError("max_hops must be between 0 and 2")
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
    visited = {seed}
    frontier = [seed]
    expanded: list[str] = []
    for _ in range(max_hops):
        next_frontier: list[str] = []
        for node in frontier:
            for target in adjacency.get(node, []):
                page = target.split("#", 1)[0]
                if page in visited:
                    continue
                visited.add(page)
                expanded.append(page)
                next_frontier.append(page)
        frontier = next_frontier
    return expanded
