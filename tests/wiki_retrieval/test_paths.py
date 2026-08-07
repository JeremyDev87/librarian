from __future__ import annotations

from pathlib import Path

import pytest

from local_wiki_librarian.config import RetrievalConfig
from local_wiki_librarian.paths import (
    UnsafePathError,
    parse_retrieval_path,
    resolve_within,
    validate_disjoint_roots,
)


def test_resolve_within_accepts_normal_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert resolve_within(root, "nested/page.md") == root / "nested" / "page.md"


def test_parse_retrieval_path_rejects_uri_schemes() -> None:
    assert parse_retrieval_path("custom://collection/page.md") == (None, "")


def test_parse_retrieval_path_strips_query_suffix() -> None:
    assert parse_retrieval_path("notes/page.md?index=1") == (None, "notes/page.md")


@pytest.mark.parametrize("relative", ["../escape.md", "nested/../../escape.md"])
def test_resolve_within_rejects_parent_traversal(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(UnsafePathError, match="parent traversal"):
        resolve_within(root, relative)


def test_resolve_within_rejects_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(UnsafePathError, match="absolute"):
        resolve_within(root, tmp_path / "outside.md")


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError, match="symlink"):
        resolve_within(root, "link/escaped.md")


def test_disjoint_roots_reject_nested_state(tmp_path: Path) -> None:
    canonical = tmp_path / "wiki"
    canonical.mkdir()

    with pytest.raises(UnsafePathError, match="must not overlap"):
        validate_disjoint_roots(canonical, canonical / ".wikimap")


def test_disjoint_roots_reject_case_alias_on_insensitive_volume(tmp_path: Path) -> None:
    canonical = tmp_path / "Wiki"
    canonical.mkdir()
    alias = tmp_path / "WIKI"
    if not alias.exists() or not canonical.samefile(alias):
        pytest.skip("case-sensitive test volume")

    with pytest.raises(UnsafePathError, match="overlap physically"):
        validate_disjoint_roots(canonical, alias / ".librarian-state")


def test_retrieval_config_validates_without_creating_state(tmp_path: Path) -> None:
    canonical = tmp_path / "wiki"
    state = tmp_path / "state"
    vendor = tmp_path / "vendor"
    canonical.mkdir()
    vendor.mkdir()

    config = RetrievalConfig(canonical_root=canonical, state_root=state, vendor_root=vendor)
    validated = config.validate()

    assert validated.canonical_root == canonical.resolve()
    assert validated.state_root == state.resolve()
    assert not state.exists()
