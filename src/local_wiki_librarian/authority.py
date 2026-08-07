"""Authority compiler: classify wiki pages into authority tiers and resolve redirects.

Reads a snapshot's Markdown files, parses frontmatter, and classifies each file:

- **current**: pages that explicitly declare ``authority: high`` and ``status: active``
- **authority**: authoritative but potentially not current (high authority, not redirected)
- **redirect**: stub pages with ``canonical_path`` pointing to the real page
- **raw**: evidence-only pages (``authority=evidence``, ``source_role=source_map``, raw/ prefix)
- **index**: catalog/log/README pages (structural, not content authority)
- **history**: superseded/discarded/stale pages
- **unknown**: no frontmatter or unclassifiable

Redirect resolution follows ``canonical_path`` to produce a resolved authority map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .frontmatter import ParsedFrontmatter, parse_frontmatter_bytes

__all__ = [
    "AuthorityEntry",
    "AuthorityManifest",
    "compile_authority",
]


def _path_policy_tier(relative_path: str) -> str | None:
    parts = Path(relative_path).parts
    folded = [part.casefold() for part in parts]
    name = folded[-1] if folded else ""
    if ".backups" in folded:
        return "history"
    if any(part.startswith(".") for part in parts):
        return "raw"
    if folded[:1] == ["raw"] or folded[:2] == ["assets", "raw"]:
        return "raw"
    if re.fullmatch(r"(?:index|log)(?: \d+)?\.md", name):
        return "index"
    return None


def _icloud_conflict_original(relative_path: str, known_paths: set[str]) -> str | None:
    """Return the canonical sibling for an local-style ``name N.md`` copy."""
    path = Path(relative_path)
    match = re.fullmatch(r"(?P<stem>.+) (?P<number>\d+)\.md", path.name, flags=re.IGNORECASE)
    if not match or int(match.group("number")) < 2:
        return None
    original = (path.parent / f"{match.group('stem')}.md").as_posix()
    return original if original in known_paths else None


def _classify_tier(
    fm: ParsedFrontmatter,
    relative_path: str = "",
    *,
    conflict_copy: bool = False,
) -> str:
    """Classify a parsed frontmatter into an authority tier."""
    path_tier = _path_policy_tier(relative_path)
    if path_tier:
        return path_tier
    # No frontmatter — unclassifiable
    if not fm.has_frontmatter:
        return "unknown"

    # Redirects first — they must never answer as current
    if fm.is_redirect:
        return "redirect"

    # Raw/evidence-only
    if fm.is_raw_or_evidence:
        return "raw"

    # Index/log/catalog
    if fm.is_index_or_log:
        return "index"

    # local conflict copies can be retained as evidence/history, never current truth.
    if conflict_copy:
        return "history"

    # History (superseded/discarded/stale)
    if fm.status in ("superseded", "discarded", "stale"):
        return "history"
    if fm.authority in ("superseded", "discarded"):
        return "history"

    # Current truth: high authority + active status
    if fm.is_current_truth:
        return "current"

    # Authoritative but not highest
    if fm.is_authoritative:
        return "authority"

    # Draft or unknown
    if fm.status == "draft":
        return "draft"

    return "unknown"


@dataclass(frozen=True)
class AuthorityEntry:
    """One file's authority classification."""

    relative_path: str
    tier: str
    wiki_schema: str | None = None
    layer: str | None = None
    domain: str | None = None
    source_role: str | None = None
    authority: str | None = None
    status: str | None = None
    do_not_answer_as_current: bool | None = None
    last_verified: str | None = None
    canonical_path: str | None = None
    redirect_reason: str | None = None
    has_frontmatter: bool = False

    def to_dict(self) -> dict:
        """Serialize to a dict for JSON manifest output."""
        d: dict = {
            "relative_path": self.relative_path,
            "tier": self.tier,
            "has_frontmatter": self.has_frontmatter,
        }
        for key in (
            "wiki_schema", "layer", "domain", "source_role",
            "authority", "status", "do_not_answer_as_current",
            "last_verified", "canonical_path", "redirect_reason",
        ):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        return d


@dataclass(frozen=True)
class AuthorityManifest:
    """Compiled authority manifest for a snapshot generation."""

    generation: str
    entries: list[AuthorityEntry] = field(default_factory=list)
    tier_counts: dict[str, int] = field(default_factory=dict)
    redirect_map: dict[str, str] = field(default_factory=dict)
    unresolved_redirects: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a dict for JSON manifest output."""
        return {
            "schema_version": 1,
            "generation": self.generation,
            "tier_counts": dict(self.tier_counts),
            "redirect_map": dict(self.redirect_map),
            "unresolved_redirects": list(self.unresolved_redirects),
            "entries": [e.to_dict() for e in sorted(self.entries, key=lambda e: e.relative_path)],
        }


def compile_authority(
    snapshot_generation_dir: Path,
    generation: str,
) -> AuthorityManifest:
    """Compile an authority manifest from a snapshot generation directory.

    Args:
        snapshot_generation_dir: Path to the generation directory containing
            ``*.md`` files and a ``manifest.json``.
        generation: The generation identifier.

    Returns:
        AuthorityManifest with per-file classification and redirect resolution.

    Raises:
        FileNotFoundError: If the generation directory doesn't exist.
    """
    gen_dir = Path(snapshot_generation_dir)
    if not gen_dir.is_dir():
        raise FileNotFoundError(f"snapshot generation not found: {gen_dir}")

    entries: list[AuthorityEntry] = []
    tier_counts: dict[str, int] = {}
    redirect_map: dict[str, str] = {}
    unresolved_redirects: list[dict] = []

    # Collect all .md files
    md_files: list[Path] = []
    for dirpath, dirnames, filenames in os_walk_sorted(gen_dir):
        for filename in filenames:
            if filename.endswith(".md"):
                md_files.append(Path(dirpath) / filename)

    # Track all known paths before classification so sibling conflict copies are deterministic.
    known_paths = {str(path.relative_to(gen_dir)) for path in md_files}

    # First pass: parse and classify
    for md_path in md_files:
        rel = str(md_path.relative_to(gen_dir))
        if rel == "manifest.json":
            continue

        try:
            raw_bytes = md_path.read_bytes()
            fm = parse_frontmatter_bytes(raw_bytes, rel)
        except Exception:
            # On any parse error, classify as unknown
            entries.append(AuthorityEntry(
                relative_path=rel,
                tier="unknown",
                has_frontmatter=False,
            ))
            tier_counts["unknown"] = tier_counts.get("unknown", 0) + 1
            continue

        tier = _classify_tier(
            fm,
            rel,
            conflict_copy=_icloud_conflict_original(rel, known_paths) is not None,
        )
        entry = AuthorityEntry(
            relative_path=rel,
            tier=tier,
            wiki_schema=fm.wiki_schema,
            layer=fm.layer,
            domain=fm.domain,
            source_role=fm.source_role,
            authority=fm.authority,
            status=fm.status,
            do_not_answer_as_current=fm.do_not_answer_as_current,
            last_verified=fm.last_verified,
            canonical_path=fm.canonical_path,
            redirect_reason=fm.redirect_reason,
            has_frontmatter=fm.has_frontmatter,
        )
        entries.append(entry)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        # Build redirect map
        if tier == "redirect" and fm.canonical_path:
            redirect_map[rel] = fm.canonical_path

    # Second pass: resolve redirects
    for src, dst in redirect_map.items():
        # Normalize: strip leading/trailing slashes, ensure .md
        normalized_dst = dst.strip("/")
        if not normalized_dst.endswith(".md"):
            # canonical_path might omit .md extension
            normalized_dst_md = normalized_dst + ".md"
        else:
            normalized_dst_md = normalized_dst

        if normalized_dst not in known_paths and normalized_dst_md not in known_paths:
            unresolved_redirects.append({
                "from": src,
                "to": dst,
                "reason": "canonical_path target not found in snapshot",
            })

    return AuthorityManifest(
        generation=generation,
        entries=entries,
        tier_counts=tier_counts,
        redirect_map=redirect_map,
        unresolved_redirects=unresolved_redirects,
    )


def os_walk_sorted(root: Path) -> Iterator[tuple[str, list[str], list[str]]]:
    """os.walk wrapper that yields sorted directories for deterministic ordering."""
    import os
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        yield dirpath, dirnames, filenames
