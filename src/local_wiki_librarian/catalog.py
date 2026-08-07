"""Compile and validate an optional routing catalog projection."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from .frontmatter import parse_frontmatter_bytes
from .paths import resolve_within

REGISTRY_PATH = Path("catalog.md")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


@dataclass(frozen=True)
class CatalogFinding:
    code: str
    message: str
    severity: str = "error"
    path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RegistryEntry:
    triggers: list[str]
    domain: str
    first_load: str
    first_loads: list[str] = field(default_factory=list)
    authority_domains: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    answer_compiler: str = ""
    row_number: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Catalog:
    source_path: str
    entries: list[RegistryEntry] = field(default_factory=list)
    findings: list[CatalogFinding] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "source_path": self.source_path,
            "valid": self.valid,
            "entries": [entry.to_dict() for entry in self.entries],
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _strip_cell(value: str) -> str:
    return value.strip().strip("`").strip()


def _wiki_path(value: str) -> str:
    paths = _wiki_paths(value)
    return paths[0] if paths else ""


def _wiki_paths(value: str) -> list[str]:
    paths: list[str] = []
    for match in _WIKILINK_RE.finditer(value):
        raw = match.group(1).strip().replace("\\", "/")
        logical = PurePosixPath(raw)
        if raw.startswith("/") or ".." in logical.parts:
            continue
        path = logical.as_posix().strip("/")
        normalized = path if path.endswith(".md") else path + ".md"
        if normalized and normalized not in paths:
            paths.append(normalized)
    return paths


def _triggers(value: str) -> list[str]:
    plain = value.replace("`", "")
    items = [item.strip() for item in re.split(r"[,，]", plain) if item.strip()]
    return items or ([plain.strip()] if plain.strip() else [])


def _collections(value: str) -> list[str]:
    return sorted(set(re.findall(r"`([^`]+)`", value)))


def compile_catalog(snapshot_root: Path) -> Catalog:
    """Parse an optional catalog table; malformed present catalogs fail closed."""
    root = Path(snapshot_root)
    registry = root / REGISTRY_PATH
    catalog = Catalog(source_path=str(REGISTRY_PATH))
    if not registry.is_file():
        return catalog
    try:
        lines = registry.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        catalog.findings.append(CatalogFinding(
            code="MALFORMED_REGISTRY", message=f"domain registry unreadable: {type(exc).__name__}", path=str(REGISTRY_PATH)
        ))
        return catalog

    header_index = -1
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if line.lstrip().startswith("|") and "trigger" in lowered and "domain" in lowered and "first load" in lowered:
            header_index = index
            break
    if header_index < 0 or header_index + 2 >= len(lines):
        catalog.findings.append(CatalogFinding(
            code="MALFORMED_REGISTRY", message="Domain route table header not found", path=str(REGISTRY_PATH)
        ))
        return catalog

    seen_domains: dict[str, int] = {}
    for line_number, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        if not line.lstrip().startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            catalog.findings.append(CatalogFinding(
                code="MALFORMED_REGISTRY", message=f"row {line_number} has {len(cells)} cells", path=str(REGISTRY_PATH)
            ))
            continue
        domain = _strip_cell(cells[1])
        owners = _wiki_paths(cells[2])
        owner = owners[0] if owners else ""
        compiler = _wiki_path(cells[4]) or _strip_cell(cells[4])
        authority_domains: set[str] = set()
        for owner_path in owners:
            target = resolve_within(root, owner_path)
            if target.is_file():
                try:
                    parsed = parse_frontmatter_bytes(target.read_bytes(), owner_path)
                except (OSError, UnicodeError, ValueError):
                    continue
                if parsed.domain:
                    authority_domains.add(parsed.domain)
        entry = RegistryEntry(
            triggers=_triggers(cells[0]),
            domain=domain,
            first_load=owner,
            first_loads=owners,
            authority_domains=sorted(authority_domains),
            collections=_collections(cells[3]),
            answer_compiler=compiler,
            row_number=line_number,
        )
        catalog.entries.append(entry)
        if domain in seen_domains:
            catalog.findings.append(CatalogFinding(
                code="DUPLICATE_ROUTE",
                message=f"domain {domain!r} appears on rows {seen_domains[domain]} and {line_number}",
                path=str(REGISTRY_PATH),
            ))
        else:
            seen_domains[domain] = line_number
        if not owner:
            catalog.findings.append(CatalogFinding(
                code="MISSING_OWNER", message=f"domain {domain!r} has no first-load Wikilink", path=str(REGISTRY_PATH)
            ))
        else:
            for owner_path in owners:
                if not resolve_within(root, owner_path).is_file():
                    catalog.findings.append(CatalogFinding(
                        code="MISSING_OWNER", message=f"domain {domain!r} owner missing", path=owner_path
                    ))

    if not catalog.entries:
        catalog.findings.append(CatalogFinding(
            code="MALFORMED_REGISTRY", message="Domain route table has no entries", path=str(REGISTRY_PATH)
        ))
    return catalog


def route_query(query: str, catalog: Catalog, requested_domain: Optional[str] = None) -> Optional[RegistryEntry]:
    """Choose a Registry route using explicit domain first, then trigger matches."""
    if requested_domain:
        for entry in catalog.entries:
            if entry.domain.casefold() == requested_domain.casefold():
                return entry
        return None
    folded = query.casefold()
    scored: list[tuple[int, int, RegistryEntry]] = []
    for index, entry in enumerate(catalog.entries):
        matches = [trigger for trigger in entry.triggers if trigger.casefold() in folded]
        if matches:
            scored.append((max(len(trigger) for trigger in matches), -index, entry))
    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2]
