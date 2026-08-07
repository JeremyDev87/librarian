"""Parse generic YAML frontmatter from Markdown knowledge-base files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "FrontmatterError",
    "ParsedFrontmatter",
    "parse_frontmatter",
    "parse_frontmatter_bytes",
]

_FRONTMATTER_RE = re.compile(
    rb"\A---\s*\n(.*?)---\s*(?:\n|$)",
    re.DOTALL,
)


class FrontmatterError(RuntimeError):
    """Raised when frontmatter parsing fails."""


def _normalize_yaml_values(data: Any) -> Any:
    """Recursively convert YAML date/datetime objects to ISO strings."""
    if isinstance(data, dict):
        return {k: _normalize_yaml_values(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_normalize_yaml_values(v) for v in data]
    if isinstance(data, datetime):
        return data.isoformat()
    if isinstance(data, date):
        return data.isoformat()
    return data


@dataclass(frozen=True)
class ParsedFrontmatter:
    """Structured frontmatter data extracted from a wiki Markdown file."""

    relative_path: str
    has_frontmatter: bool
    has_authority_schema: bool
    raw: dict[str, Any] = field(default_factory=dict)

    # Optional authority and routing fields.
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

    # Common fields
    title: str | None = None
    tags: list[str] | None = None

    @property
    def is_redirect(self) -> bool:
        """True if this page is a redirect stub that must not be answered as current."""
        if self.status == "redirected":
            return True
        if self.do_not_answer_as_current is True:
            return True
        if self.authority in ("redirect_alias", "legacy_alias"):
            return True
        return False

    @property
    def is_authoritative(self) -> bool:
        """True only when the page explicitly declares high authority."""
        if self.is_redirect:
            return False
        if self.status in ("superseded", "discarded", "stale", "draft"):
            return False
        if self.do_not_answer_as_current is True:
            return False
        return self.authority == "high"

    @property
    def is_current_truth(self) -> bool:
        """True if this page can answer current truth (high authority + active)."""
        if not self.is_authoritative:
            return False
        return self.is_authoritative and self.status == "active"

    @property
    def is_raw_or_evidence(self) -> bool:
        """True if this page is raw evidence, not authority."""
        if self.authority == "evidence":
            return True
        if self.source_role in ("source_map", "raw_source", "imported_repo_docs"):
            return True
        if self.relative_path.startswith("raw/"):
            return True
        if self.relative_path.startswith("assets/raw/"):
            return True
        if self.relative_path.startswith("assets/"):
            return True
        return False

    @property
    def is_index_or_log(self) -> bool:
        """True if this page is an index/log/catalog, not authority content."""
        basename = Path(self.relative_path).name
        if basename in ("index.md", "log.md", "CLAUDE.md", "README.md"):
            return True
        if self.relative_path.endswith("/index.md"):
            return True
        if self.relative_path.endswith("/log.md"):
            return True
        return False


def parse_frontmatter_bytes(
    raw_bytes: bytes, relative_path: str
) -> ParsedFrontmatter:
    """Parse frontmatter from raw bytes.

    Args:
        raw_bytes: The full Markdown file content as bytes.
        relative_path: Relative path of the file within the wiki root.

    Returns:
        ParsedFrontmatter with extracted fields.

    Raises:
        FrontmatterError: If YAML frontmatter is malformed.
    """
    match = _FRONTMATTER_RE.match(raw_bytes)
    if match is None:
        return ParsedFrontmatter(
            relative_path=relative_path,
            has_frontmatter=False,
            has_authority_schema=False,
        )

    yaml_bytes = match.group(1)
    try:
        data = yaml.safe_load(yaml_bytes)
    except yaml.YAMLError as exc:
        raise FrontmatterError(
            f"malformed YAML frontmatter in {relative_path}: {exc}"
        ) from exc

    if data is None:
        return ParsedFrontmatter(
            relative_path=relative_path,
            has_frontmatter=True,
            has_authority_schema=False,
        )
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"frontmatter must be a mapping in {relative_path}, got {type(data).__name__}"
        )

    # Normalize YAML date/datetime objects to ISO strings for JSON safety
    data = _normalize_yaml_values(data)

    has_authority_schema = data.get("wiki_schema") is not None

    # Normalize tags: can be a list or a comma-separated string
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tags = [str(t) for t in tags]

    do_not_answer_as_current = data.get("do_not_answer_as_current")
    if "do_not_answer_as_current" in data and not isinstance(
        do_not_answer_as_current, bool
    ):
        # Invalid exclusion metadata must never weaken the authority lock.
        do_not_answer_as_current = True

    return ParsedFrontmatter(
        relative_path=relative_path,
        has_frontmatter=True,
        has_authority_schema=has_authority_schema,
        raw=data,
        wiki_schema=data.get("wiki_schema"),
        layer=data.get("layer"),
        domain=data.get("domain"),
        source_role=data.get("source_role"),
        authority=data.get("authority"),
        status=data.get("status"),
        do_not_answer_as_current=do_not_answer_as_current,
        last_verified=data.get("last_verified"),
        canonical_path=data.get("canonical_path"),
        redirect_reason=data.get("redirect_reason"),
        title=data.get("title"),
        tags=tags,
    )


def parse_frontmatter(path: Path, root: Path) -> ParsedFrontmatter:
    """Parse frontmatter from a file on disk.

    Args:
        path: Absolute path to the Markdown file.
        root: The wiki root to compute relative_path from.

    Returns:
        ParsedFrontmatter with extracted fields.

    Raises:
        FrontmatterError: If the file cannot be read or frontmatter is malformed.
    """
    relative = str(path.relative_to(root))
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise FrontmatterError(f"cannot read {relative}: {exc}") from exc
    return parse_frontmatter_bytes(raw_bytes, relative)
