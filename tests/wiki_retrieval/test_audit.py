from __future__ import annotations

from pathlib import Path

from local_wiki_librarian.audit import AuditContext, run_catalog_audit
from local_wiki_librarian.authority import AuthorityEntry, AuthorityManifest
from local_wiki_librarian.catalog import Catalog, CatalogFinding


def test_audit_is_read_only_and_reports_drift(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.md"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = sentinel.read_bytes()
    catalog = Catalog(
        source_path="registry.md",
        findings=[CatalogFinding(code="MISSING_OWNER", message="missing")],
    )
    authority = AuthorityManifest(
        generation="g",
        entries=[
            AuthorityEntry(
                relative_path="raw/leak.md",
                tier="current",
                do_not_answer_as_current=True,
            )
        ],
        unresolved_redirects=[{"from": "a", "to": "b"}],
    )
    report = run_catalog_audit(
        AuditContext(
            catalog=catalog,
            authority=authority,
            index_collections={"wiki-domain-x": {"files": 0, "path": "/old/wiki/x"}},
            expected_collection_paths={"wiki-domain-x": "/wiki/domains/x"},
        )
    )
    codes = {finding.code for finding in report.findings}
    assert {"MISSING_OWNER", "UNSAFE_CURRENT_TIER", "UNRESOLVED_REDIRECT", "COLLECTION_EMPTY", "COLLECTION_PATH_DRIFT"} <= codes
    assert sentinel.read_bytes() == before
