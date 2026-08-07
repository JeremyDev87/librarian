from __future__ import annotations

from pathlib import Path

from local_wiki_librarian.catalog import compile_catalog


def test_missing_catalog_is_an_optional_empty_projection(tmp_path: Path) -> None:
    catalog = compile_catalog(tmp_path)

    assert catalog.valid is True
    assert catalog.entries == []
    assert catalog.findings == []


def test_present_malformed_catalog_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "catalog.md").write_text("# Not a route table\n", encoding="utf-8")

    catalog = compile_catalog(tmp_path)

    assert catalog.valid is False
    assert [finding.code for finding in catalog.findings] == ["MALFORMED_REGISTRY"]


def test_present_catalog_with_missing_owner_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "catalog.md").write_text(
        "| Trigger | Domain | First Load | Collections | Answer Compiler |\n"
        "|---|---|---|---|---|\n"
        "| policy | docs | [[missing/owner]] | `docs` | compiler |\n",
        encoding="utf-8",
    )

    catalog = compile_catalog(tmp_path)

    assert catalog.valid is False
    assert [finding.code for finding in catalog.findings] == ["MISSING_OWNER"]